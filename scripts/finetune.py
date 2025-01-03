import datetime
from functools import partial
import os
import numpy as np

from absl import app, flags, logging
import flax
from flax.traverse_util import flatten_dict
import jax
from jax.sharding import Mesh, NamedSharding, PartitionSpec
from ml_collections import config_flags, ConfigDict
import optax
import tensorflow as tf
import tqdm
import wandb

from octo.data.dataset import make_single_dataset
from octo.model.octo_model import OctoModel
from octo.utils.jax_utils import initialize_compilation_cache
from octo.utils.spec import ModuleSpec
from octo.utils.train_callbacks import (
    RolloutVisualizationCallback,
    SaveCallback,
    ValidationCallback,
    VisualizationCallback,
)
from octo.utils.train_utils import (
    check_config_diff,
    create_optimizer,
    format_name_with_config,
    merge_params,
    process_text,
    Timer,
    TrainState,
)

from octo.model_lora.octo_model import OctoModel as NewOctoModel
from octo.model_lora_v2.octo_model import OctoModel as OctoModelV2

try:
    from jax_smi import initialise_tracking  # type: ignore

    initialise_tracking()
except ImportError:
    pass

FLAGS = flags.FLAGS

flags.DEFINE_string("name", "experiment", "Experiment name.")
flags.DEFINE_bool("debug", False, "Debug config (no wandb logging)")
flags.DEFINE_string("task_name", None, "Task name to filter from OXE dataset")

default_config_file = os.path.join(
    os.path.dirname(__file__), "configs/finetune_config.py"
)
config_flags.DEFINE_config_file(
    "config",
    default_config_file,
    "File path to the training hyperparameter configuration.",
    lock_config=False,
)


def main(_):
    initialize_compilation_cache()
    devices = jax.devices()
    logging.info(
        f"""
        Octo Finetuning Script
        ======================
        Pretrained model: {FLAGS.config.pretrained_path}
        Finetuning Dataset: {FLAGS.config.dataset_kwargs.name}
        Data dir: {FLAGS.config.dataset_kwargs.data_dir}
        Task Modality: {FLAGS.config.modality}
        Finetuning Mode: {FLAGS.config.finetuning_mode}

        # Devices: {jax.device_count()}
        Batch size: {FLAGS.config.batch_size} ({FLAGS.config.batch_size // len(devices) } per device)
        # Steps: {FLAGS.config.num_steps}
    """
    )
    if FLAGS.config.save_dir is not None:
        if not tf.io.gfile.exists(FLAGS.config.save_dir):
            tf.io.gfile.makedirs(FLAGS.config.save_dir)
    finetune_mode = FLAGS.config.finetuning_mode

    #########
    #
    # Setup Jax Data Parallelism
    #
    #########

    assert (
        FLAGS.config.batch_size % len(devices) == 0
    ), f"Batch size ({FLAGS.config.batch_size}) must be divisible by the number of devices ({len(devices)})"
    assert (
        FLAGS.config.viz_kwargs.eval_batch_size % len(devices) == 0
    ), f"Eval batch size ({FLAGS.config.viz_kwargs.eval_batch_size}) must be divisible by the number of devices ({len(devices)})"

    # create a 1D mesh with a single axis named "batch"
    mesh = Mesh(jax.devices(), axis_names="batch")
    # Our batches will be data-parallel sharded -- each device will get a slice of the batch
    dp_sharding = NamedSharding(mesh, PartitionSpec("batch"))
    # Our model will be replicated across devices (we are only doing data parallelism, not model parallelism)
    replicated_sharding = NamedSharding(mesh, PartitionSpec())

    # prevent tensorflow from using GPU memory since it's only used for data loading
    tf.config.set_visible_devices([], "GPU")

    #########
    #
    # Setup WandB
    #
    #########
    from wandb_config import WANDB_API_KEY
    wandb.login(key=WANDB_API_KEY)

    # name = format_name_with_config(
    #     FLAGS.name,
    #     FLAGS.config.to_dict(),
    # )
    name = FLAGS.name
    wandb_id = "{name}_{time}".format(
        name=name,
        time=datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
    )
    wandb.init(
        config=FLAGS.config.to_dict(),
        id=wandb_id,
        name=name,
        mode="disabled" if FLAGS.debug else None,
        **FLAGS.config.wandb,
    )

    #########
    #
    # Load Pretrained model + optionally modify config
    #
    #########

    if "hypernet" in FLAGS.config.pretrained_path:
        pretrained_model = NewOctoModel.load_pretrained(
            FLAGS.config.pretrained_path,
            step=FLAGS.config.pretrained_step,
        )
    else:
        pretrained_model = OctoModel.load_pretrained(
            FLAGS.config.pretrained_path,
            step=FLAGS.config.pretrained_step,
        )
    flat_config = flax.traverse_util.flatten_dict(
        pretrained_model.config, keep_empty_nodes=True
    )
    for d_key in flax.traverse_util.flatten_dict(
        FLAGS.config.get("config_delete_keys", ConfigDict()).to_dict()
    ):
        for c_key in list(flat_config.keys()):
            if ".".join(c_key).startswith(".".join(d_key)):
                del flat_config[c_key]

    config = ConfigDict(flax.traverse_util.unflatten_dict(flat_config))
    config.update(FLAGS.config.get("update_config", ConfigDict()))
    config = config.to_dict()
    check_config_diff(config, pretrained_model.config)

    #########
    #
    # Setup Data Loader
    #
    #########

    # create text processor
    if config["text_processor"] is None:
        text_processor = None
    else:
        text_processor = ModuleSpec.instantiate(config["text_processor"])()

    def clean_text(instruction):
        if FLAGS.config.get('clean_instruction', False):
            instruction = instruction.replace(b'caddy', b'box').replace(b'mug', b'cup').replace(b'frying pan', b'pan').replace(b'moka pot', b'pot')
        if FLAGS.config.get('remove_useless_token', False):
            instruction = instruction.replace(b' the ', b' ')
        return instruction

    def process_batch(batch):
        batch['task']['language_instruction'] = [clean_text(instruction) for instruction in batch['task']['language_instruction']]
        batch = process_text(batch, text_processor)
        if FLAGS.config.get('remove_useless_token', False):
            instruction_length = batch['task']['language_instruction']['attention_mask'].sum(1)
            batch['task']['language_instruction']['input_ids'][list(range(FLAGS.config.batch_size)), instruction_length - 1] = 0
            batch['task']['language_instruction']['attention_mask'][list(range(FLAGS.config.batch_size)), instruction_length - 1] = 0
        del batch["dataset_name"]
        return batch

    standardize_fn = ModuleSpec.create(FLAGS.config["dataset_kwargs"]["standardize_fn"])
    del FLAGS.config["dataset_kwargs"]["standardize_fn"]
    FLAGS.config["dataset_kwargs"]["standardize_fn"] = standardize_fn

    dataset = make_single_dataset(
        FLAGS.config.dataset_kwargs,
        traj_transform_kwargs=FLAGS.config.traj_transform_kwargs,
        frame_transform_kwargs=FLAGS.config.frame_transform_kwargs,
        train=True,
    )

    if 'oxe' in FLAGS.config["dataset_kwargs"]["name"] and FLAGS.task_name is not None:
        # filter by task name
        def oxe_task_filter(traj):
            task_name = traj['task']['language_instruction'][0]
            return tf.equal(task_name, tf.constant(FLAGS.task_name, dtype=tf.string))
        train_dataset = dataset.filter(oxe_task_filter).cache()
        # randomly sample
        train_dataset = train_dataset.shuffle(FLAGS.config.shuffle_buffer_size).take(100).cache()
    else:
        train_dataset = dataset

    train_data_iter = (
        train_dataset.repeat()
        .unbatch()
        .shuffle(FLAGS.config.shuffle_buffer_size)
        .batch(FLAGS.config.batch_size)
        .iterator()
    )
    train_data_iter = map(process_batch, train_data_iter)
    example_batch = next(train_data_iter)
    # import matplotlib.pyplot as plt
    # plt.figure()
    # plt.imshow(example_batch['observation']['image_primary'][0, 0])
    # plt.savefig('test.png')
    # plt.close()

    #########
    #
    # Initialize the fine-tuned model and load pretrained params
    #
    #########
    print ("====== Load Model ======")

    rng = jax.random.PRNGKey(FLAGS.config.seed)
    rng, init_rng = jax.random.split(rng)
    if "hypernet" in finetune_mode:
        config["model"]["hypernet_kwargs"] = FLAGS.config["hypernet_kwargs"].to_dict()
        if "v2" in finetune_mode:
            config['model']['heads']['action']['module'] = 'octo.model_lora_v2.components.action_heads'
            config['model']['heads']['action']['kwargs']['hypernet_kwargs'] = FLAGS.config["hypernet_kwargs"].to_dict()
            model = OctoModelV2.from_config(
                config,
                example_batch,
                text_processor,
                rng=init_rng,
                dataset_statistics=dataset.dataset_statistics,
            )
        else:
            model = NewOctoModel.from_config(
                config,
                example_batch,
                text_processor,
                rng=init_rng,
                dataset_statistics=dataset.dataset_statistics,
            )
    else:
        model = OctoModel.from_config(
            config,
            example_batch,
            text_processor,
            rng=init_rng,
            dataset_statistics=dataset.dataset_statistics,
        )
    merged_params = merge_params(model.params, pretrained_model.params)
    model = model.replace(params=merged_params)

    if FLAGS.config["hypernet_kwargs"].get('initial_image_input', False) and FLAGS.config["hypernet_kwargs"].get('transfer_vit_params', False):
        
        def copy_vit_weights(x, y):
            if x.shape == y.shape:
                return y
            else:
                return y[:, :, :x.shape[2], :] # for the input layer, the channel number is not aligned
        
        pretrained_vit_params = pretrained_model.params['octo_transformer']['observation_tokenizers_primary']
        model.params['octo_transformer']['hypernet']['TaskImageTokenizer_0'] = jax.tree_map(copy_vit_weights, model.params['octo_transformer']['hypernet']['TaskImageTokenizer_0'], pretrained_vit_params)
        # shape_check = jax.tree_util.tree_all(jax.tree_map(lambda x, y: x.shape == y.shape, model.params['octo_transformer']['hypernet']['TaskImageTokenizer_0'], pretrained_vit_params))
        # assert shape_check, "The HN ViT and base model ViT do not have the same shape"
        # model.params['octo_transformer']['hypernet']['TaskImageTokenizer_0'] = copy.deepcopy(pretrained_vit_params)
        # check = jax.tree_util.tree_all(jax.tree_map(lambda x, y: ((x != y).sum() == 0), model.params['octo_transformer']['hypernet']['TaskImageTokenizer_0'], pretrained_vit_params))
        # assert check, "The HN ViT is not initialized correctly!"

    del pretrained_model

    #########
    #
    # Setup Optimizer and Train State
    #
    #########

    params = model.params
    if FLAGS.config.optimizer.frozen_keys is None:
        FLAGS.config.optimizer.frozen_keys = model.config["optimizer"]["frozen_keys"]

    tx, lr_callable, param_norm_callable = create_optimizer(
        params,
        **FLAGS.config.optimizer.to_dict(),
    )
    train_state = TrainState.create(
        model=model,
        tx=tx,
        rng=rng,
    )

    #########
    #
    # Save all metadata
    #
    #########

    if FLAGS.config.save_dir is not None:
        save_dir = tf.io.gfile.join(
            FLAGS.config.save_dir,
            FLAGS.config.wandb.project,
            FLAGS.config.wandb.group or "",
            wandb_id,
        )
        wandb.config.update(dict(save_dir=save_dir), allow_val_change=True)
        logging.info("Saving to %s", save_dir)
        save_callback = SaveCallback(save_dir)

        # Add window_size to top of config, to make eval easier
        new_config = ConfigDict(model.config)
        new_config["window_size"] = example_batch["observation"][
            "timestep_pad_mask"
        ].shape[1]
        model = model.replace(config=new_config)

        # Save finetuning config since it's not saved by SaveCallback, i.e. as part of model.save_pretrained()
        with tf.io.gfile.GFile(
            tf.io.gfile.join(save_dir, "finetune_config.json"), "w"
        ) as config_file:
            config_file.write(FLAGS.config.to_json_best_effort())
    else:
        save_dir = None
        save_callback = SaveCallback(None)
        logging.warning("save_dir not passed in, not saving checkpoints")

    example_batch_spec = jax.tree_map(
        lambda arr: (arr.shape, str(arr.dtype)), example_batch
    )
    wandb.config.update(
        dict(example_batch_spec=example_batch_spec), allow_val_change=True
    )

    #########
    #
    # Define loss, train_step, and eval_step
    #
    #########

    def loss_fn(params, batch, rng, train=True):
        bound_module = model.module.bind({"params": params}, rngs={"dropout": rng})
        transformer_embeddings, lora_params = bound_module.octo_transformer(
            batch["observation"],
            batch["task"],
            batch["observation"]["timestep_pad_mask"],
            train=train,
        )
        action_loss, action_metrics = bound_module.heads["action"].loss(
            transformer_embeddings,  # action head knows to pull out the "action" readout_key
            lora_params, 
            batch["action"],
            batch["observation"]["timestep_pad_mask"],
            batch["action_pad_mask"],
            train=train,
        )
        return action_loss, action_metrics

    # Data parallelism
    # Model is replicated across devices, data is split across devices
    @partial(
        jax.jit,
        in_shardings=[replicated_sharding, dp_sharding],
    )
    def train_step(state: TrainState, batch):
        rng, dropout_rng = jax.random.split(state.rng)
        (loss, info), grads = jax.value_and_grad(loss_fn, has_aux=True)(
            state.model.params, batch, dropout_rng, train=True
        )
        grad_norm = optax.global_norm(grads)
        updates, _ = state.tx.update(grads, state.opt_state, state.model.params)
        update_norm = optax.global_norm(updates)
        info.update(
            {
                "grad_norm": grad_norm,
                "update_norm": update_norm,
                "param_norm": param_norm_callable(state.model.params),
                "learning_rate": lr_callable(state.step),
                "training_loss": loss,
            }
        )
        new_state = state.apply_gradients(grads=grads, rng=rng)
        return new_state, info

    #########
    #
    # Build validation & visualization callbacks
    #
    #########

    if FLAGS.config.modality == "image_conditioned":
        modes_to_evaluate = ["image_conditioned"]
    elif FLAGS.config.modality == "text_conditioned":
        modes_to_evaluate = ["text_conditioned"]
    elif FLAGS.config.modality == "multimodal":
        modes_to_evaluate = ["image_conditioned", "text_conditioned"]
    else:
        modes_to_evaluate = ["base"]

    dataset_kwargs_list = [FLAGS.config.dataset_kwargs]

    val_callback = ValidationCallback(
        loss_fn=loss_fn,
        process_batch_fn=process_batch,
        text_processor=text_processor,
        val_dataset_kwargs_list=dataset_kwargs_list,
        dataset_kwargs=FLAGS.config,
        modes_to_evaluate=modes_to_evaluate,
        **FLAGS.config.val_kwargs,
    )

    viz_callback = VisualizationCallback(
        text_processor=text_processor,
        val_dataset_kwargs_list=dataset_kwargs_list,
        dataset_kwargs=FLAGS.config,
        modes_to_evaluate=modes_to_evaluate,
        **FLAGS.config.viz_kwargs,
    )

    #########
    #
    # Optionally build visualizers for sim env evals
    #
    #########

    if "rollout_kwargs" in FLAGS.config:
        rollout_callback = RolloutVisualizationCallback(
            text_processor=text_processor,
            unnormalization_statistics=dataset.dataset_statistics["action"],
            **FLAGS.config.rollout_kwargs.to_dict(),
        )
    else:
        rollout_callback = None

    #########
    #
    # Train loop
    #
    #########

    def wandb_log(info, step):
        wandb.log(flatten_dict(info, sep="/"), step=step)

    timer = Timer()
    loss_history = []
    for i in tqdm.tqdm(
        range(0, int(FLAGS.config.num_steps)),
        total=int(FLAGS.config.num_steps),
        dynamic_ncols=True,
    ):
        timer.tick("total")

        with timer("dataset"):
            batch = next(train_data_iter)

        with timer("train"):
            train_state, update_info = train_step(train_state, batch)
            loss_history.append(update_info['training_loss'].item())

        timer.tock("total")

        if (i + 1) % 1000 == 0:
            print (f'step {i + 1}, average training loss: {np.mean(loss_history)}')
            loss_history = []

        if (i + 1) % FLAGS.config.log_interval == 0:
            update_info = jax.device_get(update_info)
            wandb_log(
                {"training": update_info, "timer": timer.get_average_times()}, step=i
            )

        # if (i + 1) % FLAGS.config.eval_interval == 0:
        #     logging.info("Evaluating...")

        #     with timer("val"):
        #         val_metrics = val_callback(train_state, i + 1)
        #         wandb_log(val_metrics, step=i)

        #     with timer("visualize"):
        #         viz_metrics = viz_callback(train_state, i + 1)
        #         wandb_log(viz_metrics, step=i)

        #     if rollout_callback is not None:
        #         with timer("rollout"):
        #             rollout_metrics = rollout_callback(train_state, i + 1)
        #             wandb_log(rollout_metrics, step=i)

        if (i + 1) % FLAGS.config.save_interval == 0 and save_dir is not None:
            logging.info("Saving checkpoint...")
            save_callback(train_state, i + 1)


if __name__ == "__main__":
    app.run(main)
