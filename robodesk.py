"""Desk environment with Franka Panda arm."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os

from dm_control import mujoco
from dm_control.utils import inverse_kinematics
from dm_control.utils import transformations
import gym
import numpy as np
from PIL import Image


class RoboDesk(gym.Env):
  """Multi-task manipulation environment."""

  def __init__(self, task='open_slide', reward='dense',
               action_type='end_effector', action_repeat=1,
               episode_length=500, image_size=64):
    assert reward in ('dense', 'sparse', 'success'), reward

    try:
      from google3.pyglib import resources  # pylint: disable=g-import-not-at-top
      dir_path = '/google3/experimental/users/hkannan/world_models/'
      desk_path = 'franka_desk/assets/desk.xml'
      model_path = resources.GetARootDirWithAllResources() + os.path.join(
          dir_path, desk_path)
    except ImportError:
      model_path = 'assets/desk.xml'
    self.physics = mujoco.Physics.from_xml_path(model_path)
    self.physics_copy = self.physics.copy(share_model=True)
    self.physics_copy.data.qpos[:] = self.physics.data.qpos[:]

    # Robot constants (may not be needed)
    self.num_joints = 9
    self.joint_bounds = self.physics.model.actuator_ctrlrange.copy()

    # Environment params
    self.image_size = image_size
    self.action_type = action_type
    self.action_dim = dict(end_effector=5, joints=9)[self.action_type]
    self.reward = reward
    self.success = None

    # Action space
    self.end_effector_scale = 0.01
    self.wrist_scale = 0.02
    self.joint_scale = 0.02

    # Episode length
    self.action_repeat = action_repeat
    self.num_steps = 0
    self.episode_length = episode_length

    self.original_pos = {}
    self.previous_z_angle = None
    self.total_rotation = 0

    # pylint: disable=g-long-lambda
    self.reward_functions = {
        # Core tasks
        'open_slide': self._slide_reward,
        'open_drawer': self._drawer_reward,
        'push_green': (lambda reward_type: self._button_reward(
            'green', reward_type)),
        'stack': self._stack_reward,
        'spin': self._spin_flat_block_reward,
        'upright_block_off_table': (lambda reward_type: self._push_off_table(
            'upright_block', reward_type)),
        'flat_block_in_bin': (lambda reward_type: self._push_in_bin(
            'flat_block', reward_type)),
        'ball_in_drawer': (lambda reward_type: self._put_in_drawer(
            'ball', reward_type)),
        'flat_block_in_shelf': (lambda reward_type: self._put_in_shelf(
            'flat_block', reward_type)),
        'lift_upright_block': (lambda reward_type: self._lift_block(
            'upright_block', reward_type)),
        'lift_flat_block': (lambda reward_type: self._lift_block(
            'flat_block', reward_type)),
        'lift_ball': (lambda reward_type: self._lift_block(
            'ball', reward_type)),

        # Extra tasks
        'push_blue': (lambda reward_type: self._button_reward(
            'blue', reward_type)),
        'push_red': (lambda reward_type: self._button_reward(
            'red', reward_type)),
        'flat_block_off_table': (lambda reward_type: self._push_off_table(
            'flat_block', reward_type)),
        'ball_off_table': (lambda reward_type: self._push_off_table(
            'ball', reward_type)),
        'upright_block_in_bin': (lambda reward_type: self._push_in_bin(
            'upright_block', reward_type)),
        'ball_in_bin': (lambda reward_type: self._push_in_bin(
            'ball', reward_type)),
        'upright_block_in_drawer': (lambda reward_type: self._put_in_drawer(
            'upright_block', reward_type)),
        'flat_block_in_drawer': (lambda reward_type: self._put_in_drawer(
            'flat_block', reward_type)),
        'upright_block_in_shelf': (lambda reward_type: self._put_in_shelf(
            'upright_block', reward_type)),
        'ball_in_shelf': (lambda reward_type: self._put_in_shelf(
            'ball',reward_type)),
    }

    self.core_tasks = list(self.reward_functions)[0:12]
    self.all_tasks = list(self.reward_functions)
    self.task = task
    # pylint: enable=g-long-lambda

  @property
  def action_space(self):
    return gym.spaces.Box(-np.ones(self.action_dim), np.ones(self.action_dim))

  @property
  def observation_space(self):
    spaces = {
        'image': gym.spaces.Box(
            0, 255, (self.image_size, self.image_size, 3), np.uint8),
        'qpos_robot': gym.spaces.Box(self.joint_bounds[:, 0],
                                     self.joint_bounds[:, 1]),
        'qvel_robot': gym.spaces.Box(-np.inf, np.inf, (9,), np.float32),
        'end_effector': gym.spaces.Box(-np.inf, np.inf, (3,), np.float32),
        'qpos_objects': gym.spaces.Box(-np.inf, np.inf, (26,), np.float32),
        'qvel_objects': gym.spaces.Box(-np.inf, np.inf, (26,), np.float32)}
    return gym.spaces.Dict(spaces)

  def render(self, mode='rgb_array', resize=True):
    # params = {'distance': 1.8, 'azimuth': 90, 'elevation': -60,
    #           'crop_box': (95, 100, 420, 355), 'size': 480}
    # params = {'distance': 1.8, 'azimuth': 90, 'elevation': -60,
    #           'crop_box': (47.5, 50.0, 210.0, 177.5), 'size': 240}
    # params = {'distance': 1.8, 'azimuth': 90, 'elevation': -60,
    #           'crop_box': (23.75, 25.0, 105.0, 88.75), 'size': 120}
    params = {'distance': 1.8, 'azimuth': 90, 'elevation': -60,
              'crop_box': (16.75, 25.0, 105.0, 88.75), 'size': 120}
    # params = {'distance': 1.8, 'azimuth': 90, 'elevation': -60,
    #           'crop_box': (11.875, 12.5, 52.5, 44.375), 'size': 60}
    camera = mujoco.Camera(
        physics=self.physics, height=params['size'],
        width=params['size'], camera_id=-1)
    camera._render_camera.distance = params['distance']  # pylint: disable=protected-access
    camera._render_camera.azimuth = params['azimuth']  # pylint: disable=protected-access
    camera._render_camera.elevation = params['elevation']  # pylint: disable=protected-access
    camera._render_camera.lookat[:] = [0, 0.535, 1.1]  # pylint: disable=protected-access

    image = camera.render(depth=False, segmentation=False)
    camera._scene.free()  # pylint: disable=protected-access

    if resize:
      image = Image.fromarray(image).crop(box=params['crop_box'])
      image = image.resize([self.image_size, self.image_size],
                           resample=Image.ANTIALIAS)
      image = np.asarray(image)
    return image

  def _ik(self, pos):
    out = inverse_kinematics.qpos_from_site_pose(
        self.physics_copy, 'end_effector', pos,
        joint_names=('panda0_joint1', 'panda0_joint2', 'panda0_joint3',
                     'panda0_joint4', 'panda0_joint5', 'panda0_joint6'),
        inplace=True)
    return out.qpos[:]

  def _action_to_delta_joint(self, unscaled_value, joint_bounds):
    """Convert actions from [-1, 1] range to joint bounds."""
    joint_range = joint_bounds[1] - joint_bounds[0]
    return (((unscaled_value + 1) * joint_range) / 2) + joint_bounds[0]

  def _convert_action(self, full_action):
    """Converts action from [-1, 1] space to desired joint position."""
    full_action = np.array(full_action)
    if self.action_type == 'end_effector':
      delta_action = full_action[0:3] * self.end_effector_scale
      position = (
          self.physics.named.data.site_xpos['end_effector'] + delta_action)

      joint = self._ik(position)
      delta_wrist = self._action_to_delta_joint(full_action[3],
                                                 self.joint_bounds[6])
      joint[6] = ((self.wrist_scale * delta_wrist) +
                  self.physics.named.data.qpos[6])
      joint[6] = np.clip(joint[6], self.joint_bounds[6][0],
                         self.joint_bounds[6][1])
      joint[7] = self._action_to_delta_joint(full_action[4],
                                              self.joint_bounds[7])
      joint[8] = joint[7]
    else:
      delta_joint = [
          self._action_to_delta_joint(full_action[i], self.joint_bounds[i])
          for i in range(9)
      ]
      delta_joint = np.array(delta_joint) * self.joint_scale
      joint = self.physics.data.qpos[:self.num_joints] + delta_joint
      joint = np.clip(joint, self.joint_bounds[:, 0], self.joint_bounds[:, 1])
    return joint

  def step(self, action):
    total_reward = 0
    for _ in range(self.action_repeat):
      joint_position = self._convert_action(action)
      for _ in range(10):
        self.physics.data.ctrl[0:9] = joint_position[0:9]
        # Ensure gravity compensation stays enabled.
        self.physics.data.qfrc_applied[0:9] = self.physics.data.qfrc_bias[0:9]
        self.physics.step()
      self.physics_copy.data.qpos[:] = self.physics.data.qpos[:]

      if self.reward == 'dense':
        total_reward += self._get_task_reward(self.task, 'dense_reward')
      elif self.reward == 'sparse':
        total_reward += float(self._get_task_reward(self.task, 'success'))
      elif self.reward == 'success':
        if self.success:
          total_reward += 0  # Only give reward once in case episode continues.
        else:
          self.success = self._get_task_reward(self.task, 'success')
          total_reward += float(self.success)
      else:
        raise ValueError(self.reward)

    self.num_steps += self.action_repeat
    if self.episode_length and self.num_steps >= self.episode_length:
      done = True
    else:
      done = False
    return self._get_obs(), total_reward, done, {'discount': 1.0}

  def _get_init_robot_pos(self):
    init_joint_pose = np.array(
        [-0.30, -0.4, 0.28, -2.5, 0.13, 1.87, 0.91, 0.01, 0.01])
    init_joint_pose += 0.15 * np.random.uniform(
        low=self.physics.model.actuator_ctrlrange[:self.num_joints, 0],
        high=self.physics.model.actuator_ctrlrange[:self.num_joints, 1])
    return init_joint_pose

  def reset(self):
    """Resets environment."""
    self.success = False
    self.num_steps = 0

    self.physics.reset()

    # Randomize object positions.
    self.physics.named.data.qpos['drawer_joint'] -= 0.10 * np.random.random()
    self.physics.named.data.qpos['slide_joint'] += 0.20 * np.random.random()

    self.physics.named.data.qpos['flat_block'][0] += 0.3 * np.random.random()
    self.physics.named.data.qpos['flat_block'][1] += 0.07 * np.random.random()
    self.physics.named.data.qpos['ball'][0] += 0.48 * np.random.random()
    self.physics.named.data.qpos['ball'][1] += 0.08 * np.random.random()
    self.physics.named.data.qpos['upright_block'][0] += 0.3 * np.random.random()
    self.physics.named.data.qpos['upright_block'][1] += 0.05 * np.random.random()

    # Set robot position.
    self.physics.data.qpos[:self.num_joints] = self._get_init_robot_pos()
    self.physics.data.qvel[:self.num_joints] = np.zeros(9)

    # Relax object intersections.
    self.physics.forward()

    # Copy physics state into IK simulation.
    self.physics_copy.data.qpos[:] = self.physics.data.qpos[:]
    self.original_pos['ball'] = self.physics.named.data.xpos['ball']
    self.original_pos['upright_block'] = self.physics.named.data.xpos[
        'upright_block']
    self.original_pos['flat_block'] = self.physics.named.data.xpos['flat_block']

    self.drawer_opened = False
    return self._get_obs()

  def _did_not_move(self, block_name):
    current_pos = self.physics.named.data.xpos[block_name]
    dist = np.linalg.norm(current_pos - self.original_pos[block_name])
    return dist < 0.01

  def _total_movement(self, block_name, max_dist=5.0):
    current_pos = self.physics.named.data.xpos[block_name]
    dist = np.linalg.norm(current_pos - self.original_pos[block_name])
    return dist / max_dist

  def _get_dist_reward(self, object_pos, max_dist=4.0):
    eepos = self.physics.named.data.site_xpos['end_effector']
    dist = np.linalg.norm(eepos - object_pos)
    return 1 - (dist / max_dist)

  def _slide_reward(self, reward_type='dense_reward'):
    if reward_type == 'dense_reward':
      door_pos = self.physics.named.data.qpos['slide_joint'][0] / 0.6
      target_pos = (self.physics.named.data.site_xpos['slide_handle'] -
                    np.array([0.15, 0, 0]))
      dist_reward = self._get_dist_reward(target_pos)
      return (0.75 * door_pos) + (0.25 * dist_reward)
    elif reward_type == 'success':
      return 1 * (self.physics.named.data.qpos['slide_joint'] > 0.55)

  def _drawer_reward(self, reward_type='dense_reward'):
    if reward_type == 'dense_reward':
      drawer_pos = abs(self.physics.named.data.qpos['drawer_joint'][0]) / 0.3
      dist_reward = self._get_dist_reward(
          self.physics.named.data.geom_xpos['drawer_handle'])
      return (0.75 * drawer_pos) + (0.25 * dist_reward)
    elif reward_type == 'success':
      return 1 * (self.physics.named.data.qpos['drawer_joint'] < -0.2)

  def _button_reward(self, color, reward_type='dense_reward'):
    press_button = (
        self.physics.named.data.qpos[color + '_light'][0] < -0.00453)
    if reward_type == 'dense_reward':
      dist_reward = self._get_dist_reward(
          self.physics.named.data.xpos[color + '_button'])
      return (0.25 * press_button) + (0.75 * dist_reward)
    elif reward_type == 'success':
      return 1.0 * press_button

  def _stack_reward(self, reward_type='dense_reward'):
    target_offset = [0, 0, 0.0377804]
    current_offset = (self.physics.named.data.xpos['upright_block'] -
                      self.physics.named.data.xpos['flat_block'])

    offset_difference = np.linalg.norm(target_offset - current_offset)

    dist_reward = self._get_dist_reward(
        self.physics.named.data.xpos['upright_block'])

    if reward_type == 'dense_reward':
      return -offset_difference + dist_reward
    elif reward_type == 'success':
      return offset_difference < 0.01

  def _spin_flat_block_reward(self, reward_type='dense_reward'):
    z_angular_velocity = self.physics.named.data.qvel['flat_block'][5]
    current_quat = self.physics.named.data.xquat['flat_block']
    target_rmat = transformations.rotation_z_axis(math.pi) # TODO take initial configuration into account
    target_quat = transformations.mat_to_quat(target_rmat)
    quat_distance = transformations.quat_dist(current_quat, target_quat)

    if reward_type == 'dense_reward':
      dist_reward = self._get_dist_reward(
          self.physics.named.data.xpos['flat_block'])
      total_rotation_reward = 1.0 - quat_distance / (2 * math.pi)
      do_not_move = 1 - self._total_movement('flat_block', max_dist=0.5)
      return ((0.25 * dist_reward) + (0.25 * abs(z_angular_velocity)) +
              (0.25 * total_rotation_reward) + (0.25 * do_not_move))
    elif reward_type == 'success':
      print('total_rotation is ', self.total_rotation)
      return 1 * (quat_distance < 1e-2)

  def _push_off_table(self, block_name, reward_type='dense_reward'):
    blocks = ['flat_block', 'upright_block', 'ball']
    blocks.remove(block_name)
    if reward_type == 'dense_reward':
      block_pushed = (1 - (self.physics.named.data.xpos[block_name][2] /
                           self.original_pos[block_name][2]))
      block_0_stay_put = (1 - self._total_movement(blocks[0]))
      block_1_stay_put = (1 - self._total_movement(blocks[1]))
      reward = ((0.8 * block_pushed) + (0.1 * block_0_stay_put) +
                (0.1 * block_1_stay_put))
      reward = max(0, min(1, reward))
      dist_reward = self._get_dist_reward(
          self.physics.named.data.xpos[block_name])
      return (0.75 * reward) + (0.25 * dist_reward)
    elif reward_type == 'success':
      return 1 * ((self.physics.named.data.qpos[block_name][2] < 0.6) and
                  self._did_not_move(blocks[0]) and
                  self._did_not_move(blocks[1]))

  def _put_in_bin(self, block_name, reward_type='dense_reward'):
    pos = self.physics.named.data.xpos[block_name]
    success = (pos[0] > 0.28) and (pos[0] < 0.52) and (pos[1] > 0.38) and (
        pos[1] < 0.62) and (pos[2] > 0) and (pos[2] < 0.4)
    if reward_type == 'dense_reward':
      dist_reward = self._get_dist_reward(
          self.physics.named.data.xpos[block_name])
      return (0.5 * dist_reward) + (0.5 * float(success))
    elif reward_type == 'success':
      return 1 * success

  def _put_in_drawer(self, block_name, reward_type='dense_reward'):
    self.drawer_opened = (self.physics.named.data.qpos['drawer_joint'] < -0.15)
    pos = self.physics.named.data.xpos[block_name]
    success = (pos[0] > -0.23) and (pos[0] < 0.23) and (pos[1] < 0.54)
    if reward_type == 'dense_reward':
      if self.drawer_opened:
        dist_reward = self._get_dist_reward(
            self.physics.named.data.xpos[block_name])
        y_reward = (0.54 - pos[1]) * 4
        ball_in_drawer = ((0.33 * dist_reward) + (0.33 * y_reward) +
                          (0.34 * float(success)))
        reward = 0.5 * ball_in_drawer + 0.5 * self._drawer_reward()
        return reward
      else:
        return 0.5 * self._drawer_reward()
    elif reward_type == 'success':
      return 1 * success

  def _put_in_shelf(self, block_name, reward_type='dense_reward'):
    x_success = (self.physics.named.data.xpos[block_name][0] > 0.2)
    y_success = (self.physics.named.data.xpos[block_name][1] > 1.0)
    success = x_success and y_success
    blocks = ['flat_block', 'upright_block', 'ball']
    blocks.remove(block_name)
    if reward_type == 'dense_reward':
      target_x_y = np.array([0.4, 1.1])
      block_dist_reward = 1 - (np.linalg.norm(
          target_x_y - self.physics.named.data.xpos[block_name][0:2]))
      dist_reward = self._get_dist_reward(
          self.physics.named.data.xpos[block_name])
      block_0_stay_put = (1 - self._total_movement(blocks[0]))
      block_1_stay_put = (1 - self._total_movement(blocks[1]))
      block_in_shelf = ((0.33 * dist_reward) + (0.33 * block_dist_reward) +
                        (0.34 * float(success)))
      reward = ((0.7 * block_in_shelf) + (0.15 * block_0_stay_put) +
                (0.15 * block_1_stay_put))
      return reward
    elif reward_type == 'success':
      return 1 * success

  def _lift_block(self, block_name, reward_type='dense_reward'):
    if reward_type == 'dense_reward':
      dist_reward = self._get_dist_reward(
          self.physics.named.data.xpos[block_name])
      block_reward = (self.physics.named.data.xpos[block_name][2] -
                      self.original_pos[block_name][2]) * 5
      block_reward = max(0, min(1, block_reward))
      return (0.75 * block_reward) + (0.25 * dist_reward)
    elif reward_type == 'success':
      return 1 * (self.physics.named.data.xpos[block_name][2] > 0.87)

  def _get_task_reward(self, task, reward_type):
    reward = self.reward_functions[task](reward_type)
    reward = max(0, min(1, reward))
    return reward

  def _get_obs(self):
    return {'image': self.render(resize=True),
            'qpos_robot': self.physics.data.qpos[:self.num_joints].copy(),
            'qvel_robot': self.physics.data.qvel[:self.num_joints].copy(),
            'end_effector': self.physics.named.data.site_xpos['end_effector'],
            'qpos_objects': self.physics.data.qvel[self.num_joints:].copy(),
            'qvel_objects': self.physics.data.qvel[self.num_joints:].copy()}
