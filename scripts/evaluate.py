import argparse
import numpy as np
import os
import tensorflow as tf
import json

import simpler_env
from simpler_env.utils.env.observation_utils import get_image_from_maniskill2_obs_dict
import sapien.core as sapien

import mediapy

tasks = [
    "google_robot_pick_coke_can",
    # "google_robot_pick_horizontal_coke_can",
    # "google_robot_pick_vertical_coke_can",
    # "google_robot_pick_standing_coke_can",
    "google_robot_pick_object",
    # "google_robot_move_near_v0",
    # "google_robot_move_near_v1",
    "google_robot_move_near",
    "google_robot_open_drawer",
    # "google_robot_open_top_drawer",
    # "google_robot_open_middle_drawer",
    # "google_robot_open_bottom_drawer",
    "google_robot_close_drawer",
    # "google_robot_close_top_drawer",
    # "google_robot_close_middle_drawer",
    # "google_robot_close_bottom_drawer",
    "google_robot_place_in_closed_drawer",
    # "google_robot_place_in_closed_top_drawer",
    # "google_robot_place_in_closed_middle_drawer",
    # "google_robot_place_in_closed_bottom_drawer",
    "google_robot_place_apple_in_closed_top_drawer",
    "widowx_spoon_on_towel",
    "widowx_carrot_on_plate",
    "widowx_stack_cube",
    "widowx_put_eggplant_in_basket",
]

# prevent a single jax process from taking up all the GPU memory
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"
gpus = tf.config.list_physical_devices("GPU")
if len(gpus) > 0:
    # prevent a single tf process from taking up all the GPU memory
    tf.config.set_logical_device_configuration(
        gpus[0],
        [tf.config.LogicalDeviceConfiguration(memory_limit=3072)],
    )

def load_model(model_name, model_path, policy_setup, input_rng = 0, step=None):
    if "rt_1" in model_name:
        from simpler_env.policies.rt1.rt1_model import RT1Inference
        ckpt_path = get_rt_1_checkpoint(model_name)
        model = RT1Inference(saved_model_path=ckpt_path, policy_setup=policy_setup)
    elif "octo" in model_name:
        from octo.simpler_new.octo_model import OctoInference
        if 'hypernet' in model_path or 'vanilla_lora' in model_path:
            from octo.model_lora.octo_model import OctoModel
        else:
            from octo.model.octo_model import OctoModel
        tempmodel = OctoModel.load_pretrained(model_path, step=step)
        model = OctoInference(model=tempmodel, policy_setup=policy_setup, init_rng=input_rng)
    else:
        raise ValueError(model_name)
    return model


def evaluate(model_name, model_path, tasks, total_runs=10, rng_input = 42, base_path = './inference_results', checkpoint_step=None):

    previous_policy_setup = ''
    all_tasks_success_rate = dict()
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if model_path == 'hf://rail-berkeley/octo-base-1.5':
        save_dir = 'finetune_saves/octo-base'
        os.makedirs(save_dir, exist_ok=True)
    else:
        save_dir = model_path
    for task_name in tasks:

        if "google" in task_name:
            policy_setup = "google_robot"
        else:
            policy_setup = "widowx_bridge"

        # reduce the number of model loading
        if policy_setup != previous_policy_setup:
            model = load_model(model_name, model_path, policy_setup, rng_input, step=checkpoint_step)
        previous_policy_setup = policy_setup

        env = simpler_env.make(task_name)

        # turned off the denoiser as the colab kernel will crash if it's turned on
        sapien.render_config.rt_use_denoiser = False

        print (f'===== {task_name} =====')
        obs, reset_info = env.reset()
        instruction = env.get_language_instruction()

        success_timestep = 0
        success_count = 0
        for run in range(total_runs):
            obs, reset_info = env.reset()
            instruction = env.get_language_instruction()
            is_final_subtask = env.is_final_subtask() 

            model.reset(instruction)
            print (instruction)

            image = get_image_from_maniskill2_obs_dict(env, obs)  # np.ndarray of shape (H, W, 3), uint8
            images = [image]
            predicted_terminated, success, truncated = False, False, False
            timestep = 0
            # TODO: env._elapsed_steps or something needs to be fixed so we don't get predicted_terminated to be true early
            # TODO: support RT-1
            from collections import defaultdict
            delta = defaultdict(float)
            while not (truncated or success):
                # step the model; "raw_action" is raw model action output; "action" is the processed action to be sent into maniskill env
                raw_action, action = model.step(image, instruction)
                predicted_terminated = bool(action["terminate_episode"][0] > 0)
                if predicted_terminated:
                    if not is_final_subtask:
                        # advance the environment to the next subtask
                        predicted_terminated = False
                        env.advance_to_next_subtask()

                obs, reward, success, truncated, info = env.step(
                    np.concatenate([action["world_vector"], action["rot_axangle"], action["gripper"]])
                )

                new_instruction = env.get_language_instruction()
                if new_instruction != instruction:
                    # update instruction for long horizon tasks
                    instruction = new_instruction
                    print (instruction)
                is_final_subtask = env.is_final_subtask() 
                # update image observation
                image = get_image_from_maniskill2_obs_dict(env, obs)
                images.append(image)
                timestep += 1
            if success:
                success_count += 1
                success_timestep += timestep
            print(run+1, success_count, success_count/(run+1)*100)
            result = 'success' if success else 'fail'
            video_path = f"{save_dir}/video/{task_name}/{run}_{result}.mp4"
            os.makedirs(f'{save_dir}/video/{task_name}', exist_ok=True)
            mediapy.write_video(video_path, images, fps=10)
        env.close()
        all_tasks_success_rate[task_name] = success_count / total_runs
        print (all_tasks_success_rate)
        try:
            with open(f'{save_dir}/eval_success_rate_{timestamp}.json', 'w') as f:
                json.dump(all_tasks_success_rate, f)
        except:
            continue


if __name__ == '__main__':

    # example command: python tools/evaluate.py --model octo-base
    # example command: python tools/evaluate.py --model_path finetune_saves/PickCokeCan_vanilla_lora/octo_finetune/PickCokeCan_20240901_073905 --num_eval 50
    parser = argparse.ArgumentParser(description="A simple example of argparse")
    # Add arguments
    parser.add_argument("--model", choices=["octo-small", "octo-base", "octo-custom", "rt_1_x", "rt_1_400k"], default="octo-custom", help="The model used for evaluation")
    parser.add_argument("--model_path", type=str, default='', help="The path of the custom model (only useful for octo-custom?)")
    parser.add_argument("--num_eval", type=int, default=53, help="Number of episodes to evaluation")
    parser.add_argument("--rng_input", type=int, default=42, help="RNG for eval run")
    parser.add_argument("--base_path", type=str, default='./inference_results', help="Base path to save inference results")
    parser.add_argument("--step", type=int, default=None, help="checkpoint step to evaluate")
    
    # Parse the arguments
    args = parser.parse_args()

    evaluate(args.model, args.model_path, tasks, total_runs=args.num_eval, rng_input = args.rng_input, base_path = args.base_path, checkpoint_step=args.step)
