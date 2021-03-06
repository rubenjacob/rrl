import collections
import functools
import json
import time

import tensorflow as tf
from tensorflow.keras.mixed_precision import experimental as prec
from tensorflow_probability import distributions as tfd
import numpy as np

from rrl_praktikum.models.action_decoder import ActionDecoder
from rrl_praktikum.models.base_module import Module
from rrl_praktikum.models.conv import ConvDecoder, ConvEncoder
from rrl_praktikum.models.dense_decoder import DenseDecoder
from rrl_praktikum.models.recurrent_model import RecurrentModel
from rrl_praktikum.models.rssm import RSSM
from rrl_praktikum.models.ssm import SSM
from rrl_praktikum.utilities.adam import Adam
from rrl_praktikum.utilities import tools, summaries, episodes


class Dreamer(Module):
    def __init__(self, config, datadir, actspace, writer):
        super().__init__()
        self._c = config
        self._actspace = actspace
        self._actdim = actspace.n if hasattr(actspace, 'n') else actspace.shape[0]
        self._writer = writer
        self._random = np.random.RandomState(config.seed)
        with tf.device('cpu:0'):
            self._step = tf.Variable(tools.count_steps(datadir, config), dtype=tf.int64)
        self._should_pretrain = tools.Once()
        self._should_train = tools.Every(config.train_every)
        self._should_log = tools.Every(config.log_every)
        self._last_log = None
        self._last_time = time.time()
        self._metrics = collections.defaultdict(tf.metrics.Mean)
        self._metrics['expl_amount']  # Create variable for checkpoint.
        self._float = prec.global_policy().compute_dtype
        self._strategy = tf.distribute.MirroredStrategy()
        with self._strategy.scope():
            self._dataset = iter(self._strategy.experimental_distribute_dataset(load_dataset(datadir, self._c)))
            self._build_model()

    def __call__(self, obs, reset, state=None, training=True):
        step = self._step.numpy().item()
        tf.summary.experimental.set_step(step)
        if state is not None and reset.any():
            mask = tf.cast(1 - reset, self._float)[:, None]
            state = tf.nest.map_structure(lambda x: x * mask, state)
        if self._should_train(step):
            log = self._should_log(step)
            n = self._c.pretrain if self._should_pretrain() else self._c.train_steps
            print(f'Training for {n} steps.')
            with self._strategy.scope():
                for train_step in range(n):
                    log_images = self._c.log_images and log and train_step == 0
                    self.train(next(self._dataset), log_images)
            if log:
                self._write_summaries()
        action, state = self.policy(obs, state, training)
        if training:
            self._step.assign_add(len(reset) * self._c.action_repeat)
        return action, state

    @tf.function
    def policy(self, obs, state, training):
        if state is None:
            latent = self._dynamics.initial(len(obs['image']))
            action = tf.zeros((len(obs['image']), self._actdim), self._float)
        else:
            latent, action = state
        embed = self._encode(preprocess(obs, self._c))
        latent, _ = self._dynamics.obs_step(latent, action, embed)
        feat = self._dynamics.get_feat(latent)
        if training:
            action = self._actor(feat).sample()
        else:
            action = self._actor(feat).mode()
        action = self._exploration(action, training)
        state = (latent, action)
        return action, state

    def load(self, filename):
        super().load(filename)
        self._should_pretrain()

    @tf.function()
    def train(self, data, log_images=False):
        self._strategy.run(self._train, args=(data, log_images))

    def _train(self, data, log_images):
        with tf.GradientTape() as model_tape:
            embed = self._encode(data)
            post, prior = self._dynamics.observe(embed, data['action'])
            feat = self._dynamics.get_feat(post)
            image_pred = self._decode(feat)
            reward_pred = self._reward(feat)
            likes = tools.AttrDict()
            likes.image = tf.reduce_mean(image_pred.log_prob(data['image']))
            likes.reward = tf.reduce_mean(reward_pred.log_prob(data['reward']))
            if self._c.pcont:
                pcont_pred = self._pcont(feat)
                pcont_target = self._c.discount * data['discount']
                likes.pcont = tf.reduce_mean(pcont_pred.log_prob(pcont_target))
                likes.pcont *= self._c.pcont_scale

            model_loss = - sum(likes.values())
            if self._c.dynamics_model != 'deterministic':
                prior_dist = self._dynamics.get_dist(prior)
                post_dist = self._dynamics.get_dist(post)
                div = tf.reduce_mean(tfd.kl_divergence(post_dist, prior_dist))
                div = tf.maximum(div, self._c.free_nats)
                model_loss += self._c.kl_scale * div
            model_loss /= float(self._strategy.num_replicas_in_sync)

        with tf.GradientTape() as actor_tape:
            imag_feat = self._imagine_ahead(post)
            reward = self._reward(imag_feat).mode()
            if self._c.pcont:
                pcont = self._pcont(imag_feat).mean()
            else:
                pcont = self._c.discount * tf.ones_like(reward)
            value = self._value(imag_feat).mode()
            returns = tools.lambda_return(reward[:-1], value[:-1], pcont[:-1], bootstrap=value[-1],
                                          lambda_=self._c.disclam, axis=0)
            discount = tf.stop_gradient(tf.math.cumprod(tf.concat([tf.ones_like(pcont[:1]), pcont[:-2]], 0), 0))
            actor_loss = -tf.reduce_mean(discount * returns)
            actor_loss /= float(self._strategy.num_replicas_in_sync)

        with tf.GradientTape() as value_tape:
            value_pred = self._value(imag_feat)[:-1]
            target = tf.stop_gradient(returns)
            value_loss = -tf.reduce_mean(discount * value_pred.log_prob(target))
            value_loss /= float(self._strategy.num_replicas_in_sync)

        model_norm = self._model_opt(model_tape, model_loss)
        actor_norm = self._actor_opt(actor_tape, actor_loss)
        value_norm = self._value_opt(value_tape, value_loss)

        if tf.distribute.get_replica_context().replica_id_in_sync_group == 0:
            if self._c.log_scalars:
                if self._c.dynamics_model != 'deterministic':
                    self._scalar_summaries(data, feat, prior_dist, post_dist, likes, div, model_loss, value_loss,
                                           actor_loss, model_norm, value_norm, actor_norm)
                else:
                    self._scalar_summaries(data, feat, None, None, likes, None, model_loss, value_loss,
                                           actor_loss, model_norm, value_norm, actor_norm)
            if tf.equal(log_images, True):
                self._image_summaries(data, embed, image_pred)

    def _build_model(self):
        acts = dict(
            elu=tf.nn.elu, relu=tf.nn.relu, swish=tf.nn.swish,
            leaky_relu=tf.nn.leaky_relu)
        cnn_act = acts[self._c.cnn_act]
        act = acts[self._c.dense_act]
        self._encode = ConvEncoder(self._c.cnn_depth, cnn_act)
        if self._c.dynamics_model == 'rssm':
            self._dynamics = RSSM(self._c.stoch_size, self._c.deter_size, self._c.deter_size)
        elif self._c.dynamics_model == 'stochastic':
            self._dynamics = SSM(self._c.stoch_size, self._c.deter_size)
        elif self._c.dynamics_model == 'deterministic':
            self._dynamics = RecurrentModel(self._c.deter_size, self._c.deter_size)
        else:
            raise NotImplementedError(self._c.dynamics_model)
        self._decode = ConvDecoder(self._c.cnn_depth, cnn_act)
        self._reward = DenseDecoder((), 2, self._c.num_units, act=act)
        if self._c.pcont:
            self._pcont = DenseDecoder((), 3, self._c.num_units, 'binary', act=act)
        self._value = DenseDecoder((), 3, self._c.num_units, act=act)
        self._actor = ActionDecoder(self._actdim, 4, self._c.num_units, self._c.action_dist,
                                    init_std=self._c.action_init_std, act=act)
        model_modules = [self._encode, self._dynamics, self._decode, self._reward]
        if self._c.pcont:
            model_modules.append(self._pcont)
        Optimizer = functools.partial(Adam, wd=self._c.weight_decay, clip=self._c.grad_clip,
                                      wdpattern=self._c.weight_decay_pattern)
        self._model_opt = Optimizer('model', model_modules, self._c.model_lr)
        self._value_opt = Optimizer('value', [self._value], self._c.value_lr)
        self._actor_opt = Optimizer('actor', [self._actor], self._c.actor_lr)
        # Do a train step to initialize all variables, including optimizer
        # statistics. Ideally, we would use batch size zero, but that doesn't work
        # in multi-GPU mode.
        self.train(next(self._dataset))

    def _exploration(self, action, training):
        if training:
            amount = self._c.expl_amount
            if self._c.expl_decay:
                amount *= 0.5 ** (tf.cast(self._step, tf.float32) / self._c.expl_decay)
            if self._c.expl_min:
                amount = tf.maximum(self._c.expl_min, amount)
            self._metrics['expl_amount'].update_state(amount)
        elif self._c.eval_noise:
            amount = self._c.eval_noise
        else:
            return action
        if self._c.expl == 'additive_gaussian':
            return tf.clip_by_value(tfd.Normal(action, amount).sample(), -1, 1)
        if self._c.expl == 'completely_random':
            return tf.random.uniform(action.shape, -1, 1)
        if self._c.expl == 'epsilon_greedy':
            indices = tfd.Categorical(0 * action).sample()
            return tf.where(
                tf.random.uniform(action.shape[:1], 0, 1) < amount,
                tf.one_hot(indices, action.shape[-1], dtype=self._float),
                action)
        raise NotImplementedError(self._c.expl)

    def _imagine_ahead(self, post):
        if self._c.pcont:  # Last step could be terminal.
            post = {k: v[:, :-1] for k, v in post.items()}
        flatten = lambda x: tf.reshape(x, [-1] + list(x.shape[2:]))
        start = {k: flatten(v) for k, v in post.items()}
        policy = lambda state: self._actor(tf.stop_gradient(self._dynamics.get_feat(state))).sample()
        states = tools.static_scan(
            lambda prev, _: self._dynamics.img_step(prev, policy(prev)),
            tf.range(self._c.horizon), start)
        imag_feat = self._dynamics.get_feat(states)
        return imag_feat

    def _scalar_summaries(self, data, feat, prior_dist, post_dist, likes, div, model_loss, value_loss, actor_loss,
                          model_norm, value_norm, actor_norm):
        self._metrics['model_grad_norm'].update_state(model_norm)
        self._metrics['value_grad_norm'].update_state(value_norm)
        self._metrics['actor_grad_norm'].update_state(actor_norm)
        if self._c.dynamics_model != 'deterministic':
            self._metrics['prior_ent'].update_state(prior_dist.entropy())
            self._metrics['post_ent'].update_state(post_dist.entropy())
            self._metrics['div'].update_state(div)
        for name, logprob in likes.items():
            self._metrics[name + '_loss'].update_state(-logprob)
        self._metrics['model_loss'].update_state(model_loss)
        self._metrics['value_loss'].update_state(value_loss)
        self._metrics['actor_loss'].update_state(actor_loss)
        self._metrics['action_ent'].update_state(self._actor(feat).entropy())

    def _image_summaries(self, data, embed, image_pred):
        truth = data['image'][:6] + 0.5
        recon = image_pred.mode()[:6]
        init, _ = self._dynamics.observe(embed[:6, :5], data['action'][:6, :5])
        init = {k: v[:, -1] for k, v in init.items()}
        prior = self._dynamics.imagine(data['action'][:6, 5:], init)
        openl = self._decode(self._dynamics.get_feat(prior)).mode()
        model = tf.concat([recon[:, :5] + 0.5, openl + 0.5], 1)
        error = (model - truth + 1) / 2
        openl = tf.concat([truth, model, error], 2)
        summaries.graph_summary(self._writer, summaries.video_summary, 'agent/openl', openl)

    def _write_summaries(self):
        step = int(self._step.numpy())
        metrics = [(k, float(v.result())) for k, v in self._metrics.items()]
        if self._last_log is not None:
            duration = time.time() - self._last_time
            self._last_time += duration
            metrics.append(('fps', (step - self._last_log) / duration))
        self._last_log = step
        [m.reset_states() for m in self._metrics.values()]
        with (self._c.logdir / 'metrics.jsonl').open('a') as f:
            f.write(json.dumps({'step': step, **dict(metrics)}) + '\n')
        [tf.summary.scalar('agent/' + k, m) for k, m in metrics]
        print(f'[{step}]', ' / '.join(f'{k} {v:.1f}' for k, v in metrics))
        self._writer.flush()


def preprocess(obs, config):
    dtype = prec.global_policy().compute_dtype
    obs = obs.copy()
    with tf.device('cpu:0'):
        obs['image'] = tf.cast(obs['image'], dtype) / 255.0 - 0.5
        clip_rewards = dict(none=lambda x: x, tanh=tf.tanh)[config.clip_rewards]
        obs['reward'] = clip_rewards(obs['reward'])
    return obs


def load_dataset(directory, config):
    episode = next(episodes.load_episodes(directory, 1))
    types = {k: v.dtype for k, v in episode.items()}
    shapes = {k: (None,) + v.shape[1:] for k, v in episode.items()}
    generator = lambda: episodes.load_episodes(directory, config.train_steps, config.batch_length,
                                               config.dataset_balance)
    dataset = tf.data.Dataset.from_generator(generator, types, shapes)
    dataset = dataset.batch(config.batch_size, drop_remainder=True)
    dataset = dataset.map(functools.partial(preprocess, config=config))
    dataset = dataset.prefetch(10)
    return dataset
