import argparse
import numpy as np
import os
import tensorflow as tf
import json
import pickle

from libero.libero import get_libero_path
from libero.libero import benchmark
from libero.libero.envs import OffScreenRenderEnv

import mediapy


# prevent a single jax process from taking up all the GPU memory
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"
gpus = tf.config.list_physical_devices("GPU")
if len(gpus) > 0:
    # prevent a single tf process from taking up all the GPU memory
    tf.config.set_logical_device_configuration(
        gpus[0],
        [tf.config.LogicalDeviceConfiguration(memory_limit=3072)],
    )

def load_model(model_name, model_path, input_rng=0, step=None):
    from octo.simpler_new.octo_model import OctoInference
    if 'hypernet' in model_path or 'vanilla_lora' in model_path:
        from octo.model_lora.octo_model import OctoModel
    else:
        from octo.model.octo_model import OctoModel
    tempmodel = OctoModel.load_pretrained(model_path, step=step)
    model = OctoInference(model=tempmodel, policy_setup='libero', init_rng=input_rng)
    return model


def evaluate(model_name, model_path, tasks, seed=0, checkpoint_step=None, split='train', save_video=False):

    if model_path == 'hf://rail-berkeley/octo-base-1.5':
        eval_path = f'eval_results/libero/octo-base/{seed}'
    else:
        save_dir = 'eval_results/libero/' + '/'.join(model_path.split('/')[1:])
        eval_path = f'{save_dir}/eval_step_{checkpoint_step}/{seed}'
    os.makedirs(eval_path, exist_ok=True)

    save_file_name = f'success_rate'
    if os.path.exists(f'{eval_path}/{save_file_name}.json'):
        with open(f'{eval_path}/{save_file_name}.json', 'r') as f:
            all_tasks_success_rate = json.load(f)
    else:
        all_tasks_success_rate = dict()

    benchmark_dict = benchmark.get_benchmark_dict()
    task_suite = benchmark_dict['libero_90']()
    all_task_names = [task.name for task in task_suite.tasks]

    model = load_model(model_name, model_path, seed, step=checkpoint_step)

    with open('domains/LIBERO/task_split.pkl', 'rb') as f:
        train_tasks, test_tasks = pickle.load(f)
    if split == 'train':
        tasks = train_tasks
    else:
        tasks = test_tasks
    tasks = [all_task_names.index(task_name[:-10]) for task_name in tasks]

    for task_id in tasks:

        # retrieve a specific task
        task = task_suite.get_task(task_id)
        task_name = task.name
        task_description = task.language
        task_bddl_file = os.path.join(get_libero_path("bddl_files"), task.problem_folder, task.bddl_file)

        if task_name in all_tasks_success_rate:
            continue

        video_path = f"{eval_path}/video/{task_name}"
        os.makedirs(video_path, exist_ok=True)

        # approach 1: single process
        env_args = {
            "bddl_file_name": task_bddl_file,
            "camera_heights": 256,
            "camera_widths": 256
        }
        env = OffScreenRenderEnv(**env_args)
        env.seed(0)
        env.reset()
        init_states = task_suite.get_task_init_states(task_id) # for benchmarking purpose, we fix a set of initial states

        model.reset(task_description)

        print (f'===== {task_name} =====')
        success_count = 0
        episode_results = []
        total_runs = 1
        for run in range(total_runs):
            env.reset()
            init_state_id = run
            init_state = env.set_init_state(init_states[init_state_id])

            image = init_state['agentview_image'][::-1]  # the simulation image is up side down, need to flip manually
            images = [image]
            done = False
            for t in range(600):
                # step the model; "raw_action" is raw model action output; "action" is the processed action to be sent into maniskill env
                raw_action, action, _, _ = model.step(image)
                # TODO: action space alignment
                obs, reward, done, info = env.step(
                    np.concatenate([action["world_vector"], action["rotation_delta"], action["gripper"]])
                )
                # update image observation
                image = obs['agentview_image'][::-1]
                images.append(image)
                if done:
                    break
            success = (reward > 0)
            if success:
                success_count += 1
            episode_results.append(success)
            print(run+1, success_count, success_count/(run+1)*100)
            if save_video:
                result = 'success' if success else 'fail'
                mediapy.write_video(f'{video_path}/{run + 1}_{result}.mp4', images, fps=10)
        env.close()
        all_tasks_success_rate[task_name] = [success_count / total_runs, episode_results]
        print ({key: all_tasks_success_rate[key][0] for key in all_tasks_success_rate})
        with open(f'{eval_path}/{save_file_name}.json', 'w') as f:
            json.dump(all_tasks_success_rate, f)

        # # reset the model with the task instruction
        # model.reset(task_description)

        # # initialize the envs
        # env_args = {
        #     "bddl_file_name": task_bddl_file,
        #     "camera_heights": 256,
        #     "camera_widths": 256
        # }
        # env = SubprocVectorEnv([lambda: OffScreenRenderEnv(**env_args) for _ in range(env_num)])
        # env.seed(0)
        # env.reset()

        # # set the initial states
        # init_states = task_suite.get_task_init_states(task_id)
        # indices = np.arange(env_num) % init_states.shape[0]
        # obs = env.set_init_state(init_states[indices])
        # breakpoint()
        # image = obs['agentview_image']
        # images = [image]

        # for _ in range(5):  # simulate the physics without any actions
        #     env.step(np.zeros((env_num, 7)))

        # print (f'===== {task_name} =====')
        # dones = [False] * env_num
        # steps = 0
        # num_success = 0
        # # TODO: max steps
        # while steps < cfg.eval.max_steps:
        #     steps += 1
        #     raw_action, action = model.step(image)
        #     obs, reward, done, info = env.step(actions)
        #     # check whether succeed
        #     for k in range(env_num):
        #         dones[k] = dones[k] or done[k]
        #     if all(dones):
        #         break
        #     image = obs['agentview_image']
        #     images.append(image)

        # for k in range(env_num):
        #     num_success += int(dones[k])

        # success_rate = num_success / env_num
        # env.close()

        # if save_video:
        #     result = 'success' if success else 'fail'
        #     mediapy.write_video(f'{video_path}/{run + 1}_{result}.mp4', images, fps=10)

        # all_tasks_success_rate[task_name] = success_rate
        # print (all_tasks_success_rate)
        # with open(f'{eval_path}/{save_file_name}.json', 'w') as f:
        #     json.dump(all_tasks_success_rate, f)



if __name__ == '__main__':

    # Add arguments
    parser = argparse.ArgumentParser(description="A simple example of argparse")
    parser.add_argument("--model", choices=["octo-small", "octo-base", "octo-custom", "rt_1_x", "rt_1_400k"], default="octo-custom", help="The model used for evaluation")
    parser.add_argument("--model_path", type=str, default='', help="The path of the custom model (only useful for octo-custom?)")
    parser.add_argument("--seeds", type=str, default='0+1+2+3', help="seeds for policy and env")
    parser.add_argument("--step", type=int, default=None, help="checkpoint step to evaluate")
    parser.add_argument("--split", type=str, default='train', help="evaluate on the train or test split")
    parser.add_argument("--save_video", action='store_true', help="save evaluation video or not")
    # Parse the arguments
    args = parser.parse_args()

    seeds = [eval(seed) for seed in args.seeds.split('+')]
    tasks = [0]
    for seed in seeds:
        evaluate(args.model, args.model_path, tasks, seed=seed, checkpoint_step=args.step, split=args.split, save_video=args.save_video)
