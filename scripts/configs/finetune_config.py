from ml_collections import ConfigDict
from ml_collections.config_dict import FieldReference, placeholder

from octo.utils.spec import ModuleSpec


def get_config(config_string="head_only,language_conditioned,libero"):
    mode, task, dataset = config_string.split(",")
    assert task in ["image_conditioned", "language_conditioned", "multimodal"]
    assert mode in ["full", "head_only", "head_mlp_only", "none"]

    # Fill this in for your own dataset!

    # There should be two image keys
    # first image key should be the third-person view (None if not used)
    # and second image key should be the wrist view (None if not used)

    FINETUNING_KWARGS = {
        "name": "oxe/fractal20220817_data",
        "data_dir": "/user/octo/data",
        "image_obs_keys": {"primary": "image", "wrist": None},
        # "proprio_obs_key": "proprio",
        "language_key": "language_instruction",
        "action_proprio_normalization_type": "normal",
        # We want to avoid normalizing the gripper
        "action_normalization_mask": [True, True, True, True, True, True, False],
        # standardize_fn is dynamically loaded from a file
        # for example: "experiments/kevin/custom_standardization_transforms.py:aloha_dataset_transform"
        # "standardize_fn": ModuleSpec.create(
        #     "octo.data.oxe.oxe_standardization_transforms:rt1_dataset_transform",
        # ),
        "standardize_fn": "octo.data.oxe.oxe_standardization_transforms:rt1_dataset_transform",
        # If the default data loading speed is too slow, try these:
        "num_parallel_reads": 16,  # for reading from disk / GCS
        "num_parallel_calls": 16,  # for initial dataset construction
    }

    if mode == "full":
        frozen_keys = None
    elif mode == "head_only":
        frozen_keys = ("octo_transformer.*",)
    elif mode == "head_mlp_only":
        frozen_keys = (
            "octo_transformer.*",
            "heads_*.map_head.probe",
            "heads_*.map_head.MultiHeadDotProductAttention_0.*",
        )
    elif mode == "none":
        frozen_keys = (
            "octo_transformer.*",
            "heads_*.*",
        )
    else:
        raise ValueError("Invalid mode")

    max_steps = FieldReference(50000)
    window_size = FieldReference(default=1)
    initial_image_in_task_context = FieldReference(False)
    augment_initial_image = FieldReference(False)

    config = dict(
        pretrained_path=placeholder(str),
        pretrained_step=placeholder(int),
        batch_size=32,
        shuffle_buffer_size=10000,
        num_steps=max_steps,
        log_interval=100,
        eval_interval=5000,
        save_interval=5000,
        save_dir=placeholder(str),
        seed=42,
        wandb=dict(
            project="octo_finetune", group=placeholder(str), entity=placeholder(str)
        ),
        dataset_kwargs=FINETUNING_KWARGS,
        modality=task,
        finetuning_mode=mode,
        window_size=window_size,
        optimizer=dict(
            learning_rate=dict(
                name="cosine",
                init_value=0.0,
                peak_value=3e-4,
                warmup_steps=2000,
                decay_steps=max_steps,
                end_value=0.0,
            ),
            weight_decay=0.01,
            clip_gradient=1.0,
            frozen_keys=frozen_keys,
            grad_accumulation_steps=None,  # if you are using grad accumulation, you need to adjust max_steps accordingly
        ),
        val_kwargs=dict(
            val_shuffle_buffer_size=1000,
            num_val_batches=32,
        ),
        viz_kwargs=dict(
            eval_batch_size=32,
            trajs_for_metrics=100,
            trajs_for_viz=8,
            samples_per_state=8,
        ),
        clean_instruction=False,
        remove_useless_token=False,
    )

    if task == "image_conditioned":
        goal_relabeling_strategy = "uniform"
        keep_image_prob = 1.0
    elif task == "language_conditioned":
        goal_relabeling_strategy = None
        keep_image_prob = 0.0
    elif task == "multimodal":
        goal_relabeling_strategy = "uniform"
        keep_image_prob = 0.5
    else:
        raise ValueError("Invalid modality")

    traj_transform_kwargs = dict(
        window_size=window_size,
        action_horizon=4,
        goal_relabeling_strategy=goal_relabeling_strategy,
        task_augment_strategy="delete_task_conditioning",
        task_augment_kwargs=dict(
            keep_image_prob=keep_image_prob,
        ),
        initial_image_in_task=initial_image_in_task_context,
        # If the default data loading speed is too slow, try these:
        num_parallel_calls=16,  # for less CPU-intensive ops
    )
    if dataset == 'metaworld':
        traj_transform_kwargs["action_pad_mask"] = [True, True, True, False, False, False, True]
    else:
        traj_transform_kwargs["action_pad_mask"] = None

    workspace_augment_kwargs = dict(
        # scale is the size of the image after cropping
        # ratio is the ratio between width and height of the cropped image, i.e., ratio larger than 1 leads to wider image
        random_resized_crop=dict(scale=[0.8, 1.0], ratio=[0.9, 1.1]),
        random_brightness=[0.1],
        random_contrast=[0.9, 1.1],
        random_saturation=[0.9, 1.1],
        random_hue=[0.05],
        augment_order=[
            "random_resized_crop",
            "random_brightness",
            "random_contrast",
            "random_saturation",
            "random_hue",
        ],
    )
    wrist_augment_kwargs = dict(
        random_brightness=[0.1],
        random_contrast=[0.9, 1.1],
        random_saturation=[0.9, 1.1],
        random_hue=[0.05],
        augment_order=[
            "random_brightness",
            "random_contrast",
            "random_saturation",
            "random_hue",
        ],
    )
    frame_transform_kwargs = dict(
        resize_size={
            "primary": (256, 256),  # workspace (3rd person) camera is at 256x256
            "wrist": (128, 128),  # wrist camera is at 128x128
        },
        image_augment_kwargs=dict(
            primary=workspace_augment_kwargs,
            wrist=wrist_augment_kwargs,
        ),
        augment_initial_image=augment_initial_image,
        num_parallel_calls=16,  # for less CPU-intensive ops
    )
    # If the default data loading speed is too slow, try these:
    config[
        "frame_transform_threads"
    ] = 16  # for the most CPU-intensive ops (decoding, resizing, augmenting)

    config["traj_transform_kwargs"] = traj_transform_kwargs
    config["frame_transform_kwargs"] = frame_transform_kwargs

    config["hypernet_kwargs"] = dict(
        lora_type='hypernet', # "hypernet", or "vanilla"
        encoder_type='transformer', # "transformer", "mlp"
        context_embedding_dim=128,
        lora_rank=32,
        lora_alpha=1.,
        context_encoder_kwargs=dict(
            num_layers=1,
            mlp_dim=256,
            num_attention_heads=4,
            dropout_rate=0.0,
            attention_dropout_rate=0.0,
            add_position_embedding=False,
        ),
        mlp_context_encoder_layer_num=1,
        attend_to_padding=True,
        task_attend_to_layer=False,
        embedding_dropout_rate=0.0,
        diffusion_lora=False,
        separate_token_for_lora_module=False,
        layer_token_self_attention=True,
        separate_token_for_base_layers=True,
        initial_image_input=initial_image_in_task_context,
        transfer_vit_params=False,
        augment_initial_image=augment_initial_image,
        scale_context_embedding=False,
    )

    return ConfigDict(config)
