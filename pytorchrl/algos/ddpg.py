import copy

import numpy as np
import pyprind

from rllab.algos.base import RLAlgorithm
from rllab.algos.ddpg import SimpleReplayPool
from rllab.misc import ext
import rllab.misc.logger as logger


import torch
from torch.autograd import Variable
import torch.nn as nn
from torch import optim

from pytorchrl.misc.tensor_utils import running_average_tensor_list
from pytorchrl.sampler import parallel_sampler

class DDPG(RLAlgorithm):
    def __init__(
        self,
        env,
        policy,
        qf,
        es,
        batch_size=32,
        n_epochs=200,
        epoch_length=1000,
        min_pool_size=10000,
        replay_pool_size=1000000,
        discount=0.99,
        max_path_length=250,
        qf_weight_decay=0.,
        qf_update_method=optim.Adam,
        qf_learning_rate=1e-3,
        policy_weight_decay=0,
        policy_update_method=optim.Adam,
        policy_learning_rate=1e-4,
        eval_samples=10000,
        soft_target=True,
        soft_target_tau=0.001,
        n_updates_per_sample=1,
        scale_reward=1.0,
        include_horizon_terminal_transitions=False,
        plot=False,
        pause_for_plot=False,
    ):
        """
        """
        self.env = env
        self.observation_dim = np.prod(env.observation_space.shape)
        self.action_dim = np.prod(env.action_space.shape)
        self.policy = policy
        self.qf = qf
        self.es = es
        self.batch_size = batch_size
        self.n_epoch = n_epochs
        self.epoch_length = epoch_length
        self.min_pool_size = min_pool_size
        self.replay_pool_size = replay_pool_size
        self.discount = discount
        self.max_path_length = max_path_length
        self.qf_weight_decay = qf_weight_decay
        # The update method and learning are using below
        self.eval_samples = eval_samples
        self.soft_target_tau = soft_target_tau
        self.n_updates_per_sample = n_updates_per_sample
        self.include_horizon_terminal_transitions = include_horizon_terminal_transitions

        self.plot = plot
        self.pause_for_plot = pause_for_plot

        # Target network
        self.target_qf = copy.deepcopy(self.qf)
        self.target_policy = copy.deepcopy(self.policy)

        # Define optimizer
        self.qf_optimizer = qf_update_method(self.qf.parameters(),
            lr=qf_learning_rate, weight_decay=self.qf_weight_decay)
        self.policy_optimizer = policy_update_method(self.policy.parameters(),
            lr=policy_learning_rate)

        self.qf_loss_averages = []
        self.policy_surr_averages = []
        self.q_averages = []
        self.y_averages = []
        self.paths = []
        self.es_path_returns = []
        self.paths_samples_cnt = 0

        self.scale_reward = scale_reward

    def start_worker(self):
        parallel_sampler.populate_task(self.env, self.policy)
        if self.plot:
            plotter.init_plot(self.env, self.policy)

    def train(self):
        pool = SimpleReplayPool(
            max_pool_size=self.replay_pool_size,
            observation_dim=self.observation_dim,
            action_dim=self.action_dim
        )

        self.start_worker()

        itr = 0
        path_length = 0
        path_return = 0
        terminal = False
        observation = self.env.reset()

        for epoch in range(self.n_epoch):
            logger.push_prefix('epoch #%d | ' % epoch)
            logger.log("Training started")
            for epoch_itr in pyprind.prog_bar(range(self.epoch_length)):
                # Execute policy
                if terminal:  # or path_length > self.max_path_length:
                    # Note that if the last time step ends an episode, the very
                    # last state and observation will be ignored and not added
                    # to the replay pool
                    observation = self.env.reset()
                    self.es.reset()
                    self.es_path_returns.append(path_return)
                    path_length = 0
                    path_return = 0

                action = self.es.get_action(itr, observation, policy=self.policy)

                next_observation, reward, terminal, _ = self.env.step(action)

                if self.plot:
                    self.env.render()

                path_length += 1
                path_return += reward

                if not terminal and path_length >= self.max_path_length:
                    terminal = True
                    # only include the terminal transition in this case if the flag was set
                    if self.include_horizon_terminal_transitions:
                        pool.add_sample(observation, action, reward * self.scale_reward, terminal)
                else:
                    pool.add_sample(observation, action, reward * self.scale_reward, terminal)

                observation = next_observation

                if pool.size >= self.min_pool_size:
                    for update_itr in range(self.n_updates_per_sample):
                        # Train policy
                        batch = pool.random_batch(self.batch_size)
                        self.do_training(itr, batch)

                itr += 1

            logger.log("Training finished")
            if pool.size >= self.min_pool_size:
                self.evaluate(epoch, pool)
                params = self.get_epoch_snapshot(epoch)
                logger.save_itr_params(epoch, params)
            logger.dump_tabular(with_prefix=False)
            logger.pop_prefix()
        self.env.terminate()

    def do_training(self, itr, batch):
        # Update Q Function
        obs, actions, rewards, next_obs, terminals = ext.extract(
            batch,
            "observations", "actions", "rewards", "next_observations",
            "terminals"
        )

        next_actions, _ = self.target_policy.get_action(next_obs)
        next_qvals = self.target_qf.get_qval(next_obs, next_actions)

        rewards = rewards.reshape(-1, 1)
        terminals_mask = (1.0 - terminals).reshape(-1, 1)
        ys = rewards + terminals_mask * self.discount * next_qvals

        self.train_qf(ys, obs, actions)
        self.train_policy(obs)

        self.target_policy.set_param_values(
            running_average_tensor_list(
                self.target_policy.get_param_values(),
                self.policy.get_param_values(),
                self.soft_target_tau))

        self.target_qf.set_param_values(
            running_average_tensor_list(
                self.target_qf.get_param_values(),
                self.qf.get_param_values(),
                self.soft_target_tau))

    def train_qf(self, expected_qval, obs_val, actions_val):
        """
        Given the mini-batch, fit the Q-value with L2 norm (defined
        in optimizer).

        Parameters
        ----------
        expected_qval (numpy.ndarray): expected q values in numpy array
            form.
        obs_val (numpy.ndarray): states draw from mini-batch, should have
            the same amount of rows as expected_qval.
        actions_val (numpy.ndarray): actions draw from mini-batch, should
            have the same amount of rows as expected_qval.
        """
        # Create Variable for input and output, we do not need gradient
        # of loss with respect to these variables
        obs = Variable(torch.from_numpy(obs_val)).type(
            torch.FloatTensor)
        actions = Variable(torch.from_numpy(actions_val)).type(
            torch.FloatTensor)
        expected_q = Variable(torch.from_numpy(expected_qval)).type(
            torch.FloatTensor)

        # Define loss function
        loss_fn = nn.MSELoss()
        loss = loss_fn(self.qf(obs, actions), expected_q)

        # Backpropagation and gradient descent
        self.qf_optimizer.zero_grad()
        loss.backward()
        self.qf_optimizer.step()

    def train_policy(self, obs_val):
        """
        Given the mini-batch, do gradient ascent on policy
        """
        obs = Variable(torch.from_numpy(obs_val)).type(torch.FloatTensor)

        # Do gradient descent, so need to add minus in front
        average_q = -self.qf(obs, self.policy(obs)).mean()

        self.policy_optimizer.zero_grad()
        average_q.backward()
        self.policy_optimizer.step()

    def evaluate(self, epoch, pool):
        logger.log("Collecting samples for evaluation")
        paths = parallel_sampler.sample_paths(
            policy_params=self.policy.get_param_values(),
            max_samples=self.eval_samples,
            max_path_length=self.max_path_length,
        )


    def get_epoch_snapshot(self, epoch):
        return dict(
            env=self.env,
            epoch=epoch,
            qf=self.qf,
            policy=self.policy,
            target_qf=self.target_qf,
            target_policy=self.target_policy,
            es=self.es)
