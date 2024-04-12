from datetime import datetime
import functools
from time import time
from typing import Tuple, Any, Mapping, Sequence, Union, Iterable, Callable
import json
from dataclasses import dataclass

from absl import app, flags, logging
import jax
import jax.numpy as jnp
from jax.tree_util import PyTreeDef
import numpy as np
import optax
import flax.linen as nn
import flax.typing
from flax.training import orbax_utils
from flax.training.train_state import TrainState
import orbax.checkpoint

from graphneuralpdesolver.experiments import DIR_EXPERIMENTS
from graphneuralpdesolver.autoregressive import AutoregressivePredictor, OperatorNormalizer
from graphneuralpdesolver.dataset import shuffle_arrays, Dataset
from graphneuralpdesolver.models.graphneuralpdesolver import GraphNeuralPDESolver, AbstractOperator
from graphneuralpdesolver.utils import disable_logging, Array
from graphneuralpdesolver.metrics import mse, rel_l2_error, rel_l1_error


SEED = 44
NUM_DEVICES = jax.local_device_count()

# FLAGS::general
FLAGS = flags.FLAGS
flags.DEFINE_string(name='datadir', default=None, required=True,
  help='Path of the folder containing the datasets'
)
flags.DEFINE_string(name='params', default=None, required=False,
  help='Path of the previous experiment containing the initial parameters'
)
flags.DEFINE_string(name='experiment', default=None, required=True,
  help='Name of the experiment: {"bm", "sin", "gauss", ...}'
)

# FLAGS::training
flags.DEFINE_integer(name='batch_size', default=4, required=False,
  help='Size of a batch of training samples'
)
flags.DEFINE_integer(name='epochs', default=20, required=False,
  help='Number of training epochs'
)
flags.DEFINE_float(name='lr', default=1e-04, required=False,
  help='Training learning rate'
)
flags.DEFINE_float(name='lr_decay', default=None, required=False,
  help='The minimum learning rate decay in the cosine scheduler'
)
flags.DEFINE_integer(name='jump_steps', default=1, required=False,
  help='Factor by which the dataset time delta is multiplied in prediction'
)
flags.DEFINE_integer(name='direct_steps', default=1, required=False,
  help='Maximum number of time steps between input/output pairs during training'
)
flags.DEFINE_integer(name='unroll_steps', default=0, required=False,
  help='Number of steps for getting a noisy input and applying the model autoregressively'
)
flags.DEFINE_integer(name='n_train', default=(2**11), required=False,
  help='Number of training samples'
)
flags.DEFINE_integer(name='n_valid', default=(2**9), required=False,
  help='Number of validation samples'
)
flags.DEFINE_integer(name='n_test', default=(2**9), required=False,
  help='Number of test samples'
)

# FLAGS::model
flags.DEFINE_integer(name='num_mesh_nodes', default=64, required=False,
  help='Number of mesh nodes in each dimension'
)
flags.DEFINE_float(name='overlap_factor_grid2mesh', default=2.0, required=False,
  help='Overlap factor for grid2mesh edges (encoder)'
)
flags.DEFINE_float(name='overlap_factor_mesh2grid', default=2.0, required=False,
  help='Overlap factor for mesh2grid edges (decoder)'
)
flags.DEFINE_integer(name='num_multimesh_levels', default=4, required=False,
  help='Number of multimesh connection levels (processor)'
)
flags.DEFINE_integer(name='latent_size', default=128, required=False,
  help='Size of latent node and edge features'
)
flags.DEFINE_integer(name='num_mlp_hidden_layers', default=2, required=False,
  help='Number of hidden layers of all MLPs'
)
flags.DEFINE_integer(name='num_message_passing_steps', default=6, required=False,
  help='Number of message-passing steps in the processor'
)

@dataclass
class EvalMetrics:
  error_autoreg_l1: float = None
  error_autoreg_l2: float = None
  error_direct_l1: float = None
  error_direct_l2: float = None

DIR = DIR_EXPERIMENTS / datetime.now().strftime('%Y%m%d-%H%M%S.%f')

def train(key: flax.typing.PRNGKey, model: nn.Module, state: TrainState, dataset: Dataset,
  jump_steps: int, direct_steps: int, unroll_steps: int, epochs: int,
  epochs_before: int = 0, loss_fn: Callable = mse) -> TrainState:
  """Trains a model and returns the state."""

  # Samples
  sample_traj, sample_spec = dataset.sample
  _use_specs = (sample_spec is not None)
  sample_traj = jax.device_put(jnp.array(sample_traj))
  sample_spec = jax.device_put(jnp.array(sample_spec)) if _use_specs else None

  # Set constants
  num_samples_trn = dataset.nums['train']
  len_traj = sample_traj.shape[1]
  num_grid_points = sample_traj.shape[2:4]
  num_vars = sample_traj.shape[-1]
  unroll_offset = unroll_steps * direct_steps
  assert num_samples_trn % FLAGS.batch_size == 0
  num_batches = num_samples_trn // FLAGS.batch_size
  assert (jump_steps * FLAGS.batch_size) % NUM_DEVICES == 0
  batch_size_per_device = (jump_steps * FLAGS.batch_size) // NUM_DEVICES
  assert (len_traj - 1) % jump_steps == 0
  num_times = (len_traj - 1) // jump_steps  # TODO: Support J08

  # Store the initial time
  time_int_pre = time()

  # Set the normalization statistics
  stats_trj_mean = jax.device_put(jnp.array(dataset.mean_trj))
  stats_trj_std = jax.device_put(jnp.array(dataset.std_trj))
  stats_res_mean = jax.device_put(jnp.array(dataset.mean_res))
  stats_res_std = jax.device_put(jnp.array(dataset.std_res))

  # Define the permissible lead times
  num_lead_times = num_times - unroll_offset - direct_steps
  assert num_lead_times > 0
  lead_times = jnp.arange(unroll_offset, num_times - direct_steps)

  # Define the autoregressive predictor
  normalizer = OperatorNormalizer(
    operator=model,
    stats_trj=(stats_trj_mean, stats_trj_std),
    stats_res=(stats_res_mean, stats_res_std),
  )
  predictor = AutoregressivePredictor(operator=normalizer, num_steps_direct=direct_steps, ndt_base=jump_steps)

  def _compute_loss(
    params: flax.typing.Collection, specs: Array,
    u_lag: Array, ndt: int, u_tgt: Array, num_steps_autoreg: int) -> Array:
    """Computes the prediction of the model and returns its loss."""

    variables = {'params': params}
    # Apply autoregressive steps
    u_inp = predictor.jump(
      variables=variables,
      specs=specs,
      u_inp=u_lag,
      num_jumps=num_steps_autoreg,
    )
    # Get the output
    # NOTE: using checkpointed version to avoid memory exhaustion
    # TRY: Change it back to model.apply to get more performance, although it is only one step..
    # u_prd = predictor._apply_operator(variables, specs=specs, u_inp=u_inp, ndt=ndt)

    _loss_inputs = normalizer.get_loss_inputs(
      variables=variables,
      specs=specs,
      u_inp=u_inp,
      u_tgt=u_tgt,
      ndt=ndt,
    )

    return loss_fn(*_loss_inputs)

  def _get_noisy_input(
    params: flax.typing.Collection, specs: Array,
    u_lag: Array, num_steps_autoreg: int) -> Array:
    """Apply the model to the lagged input to get a noisy input."""

    variables = {'params': params}
    u_inp_noisy = predictor.jump(
      variables=variables,
      specs=specs,
      u_inp=u_lag,
      num_jumps=num_steps_autoreg,
    )

    return u_inp_noisy

  def _get_loss_and_grads(
    params: flax.typing.Collection, specs: Array,
    u_lag: Array, u_tgt: Array, ndt: int) -> Tuple[Array, PyTreeDef]:
    """
    Computes the loss and the gradients of the loss w.r.t the parameters.
    """

    # Split the unrolling steps randomly to cut the gradients along the way
    # MODIFY: Change to JAX-generated random number (reproducability)
    noise_steps = np.random.choice(unroll_steps+1)
    grads_steps = unroll_steps - noise_steps

    # Get noisy input
    u_inp = _get_noisy_input(
      params, specs, u_lag, num_steps_autoreg=noise_steps)
    # Use noisy input and compute gradients
    loss, grads = jax.value_and_grad(_compute_loss)(
      params, specs, u_inp, ndt, u_tgt, num_steps_autoreg=grads_steps)

    return loss, grads

  def _get_loss_and_grads_direct_step(
    state: TrainState, key: flax.typing.PRNGKey,
    specs: Array, u_lag: Array, u_tgt: Array, ndt: int,
  ) -> Tuple[Array, PyTreeDef]:
    # NOTE: INPUT SHAPES [batch_size_per_device * num_lead_times, ...]

    # Shuffle the input/outputs along the batch axis
    if _use_specs:
      specs, u_lag, u_tgt = shuffle_arrays(key, [specs, u_lag, u_tgt])
    else:
      u_lag, u_tgt = shuffle_arrays(key, [u_lag, u_tgt])

    # Split into num_lead_times chunks and get loss and gradients
    # -> [num_lead_times, batch_size_per_device, ...]
    specs = jnp.stack(jnp.split(specs, num_lead_times)) if _use_specs else None
    u_lag = jnp.stack(jnp.split(u_lag, num_lead_times))
    u_tgt = jnp.stack(jnp.split(u_tgt, num_lead_times))

    # Add loss and gradients for each mini batch
    def _update_loss_and_grads_lead_time(i, carry):
      _loss_carried, _grads_carried = carry
      _loss_lead_time, _grads_lead_time = _get_loss_and_grads(
        params=state.params,
        specs=(specs[i] if _use_specs else None),
        u_lag=u_lag[i],
        u_tgt=u_tgt[i],
        ndt=ndt,
      )
      _loss_updated = _loss_carried + _loss_lead_time / num_lead_times
      _grads_updated = jax.tree_map(
        lambda g_old, g_new: (g_old + g_new / num_lead_times),
        _grads_carried,
        _grads_lead_time
      )
      return _loss_updated, _grads_updated

    # Loop over lead_times
    _init_loss = 0.
    _init_grads = jax.tree_map(lambda p: jnp.zeros_like(p), state.params)
    loss, grads = jax.lax.fori_loop(
      lower=0,
      upper=num_lead_times,
      body_fun=_update_loss_and_grads_lead_time,
      init_val=(_init_loss, _init_grads)
    )

    return loss, grads

  @functools.partial(jax.pmap,
    in_axes=(None, 0, 0, None),
    out_axes=(None, None, None),
    axis_name="device",
  )
  def _train_one_batch(
    state: TrainState, trajs: Array, specs: Array,
    key: flax.typing.PRNGKey) -> Tuple[TrainState, Array, Array]:
    """Loads a batch, normalizes it, updates the state based on it, and returns it."""

    # Get input output pairs for all lead times
    # -> [num_lead_times, batch_size_per_device, ...]
    u_lag_batch = jax.vmap(
        lambda lt: jax.lax.dynamic_slice_in_dim(
          operand=trajs,
          start_index=(lt-unroll_offset), slice_size=1, axis=1)
    )(lead_times)
    u_tgt_batch = jax.vmap(
        lambda lt: jax.lax.dynamic_slice_in_dim(
          operand=trajs,
          start_index=(lt+1), slice_size=direct_steps, axis=1)
    )(lead_times)
    specs_batch = (specs[None, :, :]
      .repeat(repeats=num_lead_times, axis=0)
    ) if _use_specs else None

    # Concatenate lead times along the batch axis
    # -> [batch_size_per_device * num_lead_times, ...]
    u_lag_batch = u_lag_batch.reshape(
        (batch_size_per_device * num_lead_times), 1, *num_grid_points, -1)
    u_tgt_batch = u_tgt_batch.reshape(
        (batch_size_per_device * num_lead_times), direct_steps, *num_grid_points, -1)
    specs_batch = specs_batch.reshape(
        (batch_size_per_device * num_lead_times), -1) if _use_specs else None

    # Compute loss and gradient by mapping on the time axis
    # Same u_lag and specs, loop over ndt
    key, subkey = jax.random.split(key)
    subkeys = jnp.stack(jax.random.split(subkey, num=direct_steps))
    ndt_batch = jump_steps * (1 + jnp.arange(direct_steps))  # -> [direct_steps,]
    u_tgt_batch = jnp.expand_dims(u_tgt_batch, axis=2).swapaxes(0, 1)  # -> [direct_steps, ...]

    # Shuffle direct_steps
    # NOTE: Redundent because we apply gradients on the whole batch
    # key, subkey = jax.random.split(key)
    # ndt_batch, u_tgt_batch = shuffle_arrays(subkey, [ndt_batch, u_tgt_batch])

    # Add loss and gradients for each direct_step
    def _update_loss_and_grads_direct_step(i, carry):
      _loss_carried, _grads_carried = carry
      _loss_direct_step, _grads_direct_step = _get_loss_and_grads_direct_step(
        state=state,
        key=subkeys[i],
        specs=(specs_batch if _use_specs else None),
        u_lag=u_lag_batch,
        u_tgt=u_tgt_batch[i],
        ndt=ndt_batch[i],
      )
      _loss_updated = _loss_carried + _loss_direct_step / direct_steps
      _grads_updated = jax.tree_map(
        lambda g_old, g_new: (g_old + g_new / direct_steps),
        _grads_carried,
        _grads_direct_step,
      )
      return _loss_updated, _grads_updated

    # Loop over the direct_steps
    _init_loss = 0.
    _init_grads = jax.tree_map(lambda p: jnp.zeros_like(p), state.params)
    loss, grads = jax.lax.fori_loop(
      lower=0,
      upper=direct_steps,
      body_fun=_update_loss_and_grads_direct_step,
      init_val=(_init_loss, _init_grads)
    )

    # Synchronize loss and gradients
    grads = jax.lax.pmean(grads, axis_name="device")
    loss = jax.lax.pmean(loss, axis_name="device")

    # Apply gradients
    state = state.apply_gradients(grads=grads)

    return state, loss, grads

  def train_one_epoch(
    state: TrainState, batches: Iterable[Tuple[Array, Array]],
    key: flax.typing.PRNGKey) -> Tuple[TrainState, Array, Array]:
    """Updates the state based on accumulated losses and gradients."""

    # Loop over the batches
    loss_epoch = 0.
    grad_epoch = 0.
    for batch in batches:
      # Unwrap the batch
      # -> [batch_size, len_traj, ...]
      batch = jax.tree_map(jax.device_put, batch)  # Transfer to device memory
      trajs, specs = batch

      # Downsample the trajectories
      # -> [batch_size * jump_steps, num_times, ...]
      # NOTE: The last timestep is excluded to make the length of all the trajectories even
      trajs = jnp.concatenate(jnp.split(
          (trajs[:, :-1]
          .reshape(FLAGS.batch_size, (len_traj-1) // jump_steps, jump_steps, *num_grid_points, num_vars)
          .swapaxes(1, 2)
          .reshape(FLAGS.batch_size, (len_traj-1), *num_grid_points, num_vars)),
          jump_steps,
          axis=1),
        axis=0,
      )
      specs = (jnp.tile(specs, jump_steps)
        .reshape(FLAGS.batch_size, jump_steps, -1)
        .swapaxes(0, 1)
        .reshape(FLAGS.batch_size * jump_steps, -1)
      ) if _use_specs else None

      # Split the batch between devices
      # -> [NUM_DEVICES, batch_size_per_device, ...]
      trajs = jnp.concatenate(jnp.split(jnp.expand_dims(
        trajs, axis=0), NUM_DEVICES, axis=1), axis=0)
      specs = jnp.concatenate(jnp.split(jnp.expand_dims(
        specs, axis=0), NUM_DEVICES, axis=1), axis=0) if _use_specs else None

      # Get loss and updated state
      subkey, key = jax.random.split(key)
      state, loss, grads = _train_one_batch(state, trajs, specs, subkey)
      loss_epoch += loss * FLAGS.batch_size / num_samples_trn
      grad_epoch += np.mean(jax.tree_util.tree_flatten(jax.tree_map(jnp.mean, jax.tree_map(jnp.abs, grads)))[0]) / num_batches

    return state, loss_epoch, grad_epoch

  @functools.partial(jax.pmap,
      in_axes=(None, 0, 0, None),
      static_broadcasted_argnums=(3,))
  def _predict_trajectory_autoregressively(
      state: TrainState, specs: Array, u_inp: Array, num_steps: int,
    ) -> Array:
    """
    Predicts the trajectories autoregressively.
    The input dataset must be raw (not normalized).
    """

    # Get predictions
    variables = {'params': state.params}
    rollout, _ = predictor.unroll(
      variables=variables,
      specs=specs,
      u_inp=u_inp,
      num_steps=num_steps,
    )

    return rollout

  @functools.partial(jax.pmap, in_axes=(None, 0, 0))
  def _evaluate_direct_prediction(
    state: TrainState, trajs: Array, specs: Array) -> Tuple[Array, Array]:

    # Inputs are of shape [batch_size_per_device, ...]

    # Set lead times
    num_lead_times = num_times - direct_steps
    lead_times = jnp.arange(num_times - direct_steps)

    # Get input output pairs for all lead times
    # -> [num_lead_times, batch_size_per_device, ...]
    u_inp = jax.vmap(
        lambda lt: jax.lax.dynamic_slice_in_dim(
          operand=trajs,
          start_index=(lt), slice_size=1, axis=1)
    )(lead_times)
    u_tgt = jax.vmap(
        lambda lt: jax.lax.dynamic_slice_in_dim(
          operand=trajs,
          start_index=(lt+1), slice_size=direct_steps, axis=1)
    )(lead_times)
    specs = (jnp.array(specs[None, :, :])
      .repeat(repeats=num_lead_times, axis=0)
    ) if _use_specs else None

    def get_direct_errors(lt, carry):
      err_l1_mean, err_l2_mean = carry
      def get_direct_prediction(ndt, forcing):
        u_prd = normalizer.apply(
          variables={'params': state.params},
          specs=(specs[lt] if _use_specs else None),
          u_inp=u_inp[lt],
          ndt=ndt,
        )
        return (ndt+jump_steps), u_prd
      _, u_prd = jax.lax.scan(
        f=get_direct_prediction,
        init=jump_steps, xs=None, length=direct_steps,
      )
      u_prd = u_prd.squeeze(axis=2).swapaxes(0, 1)
      err_l1_mean += jnp.sqrt(jnp.sum(jnp.power(rel_l1_error(u_prd, u_tgt[lt]), 2), axis=1)) / num_lead_times
      err_l2_mean += jnp.sqrt(jnp.sum(jnp.power(rel_l2_error(u_prd, u_tgt[lt]), 2), axis=1)) / num_lead_times

      return err_l1_mean, err_l2_mean

    # Get mean errors per each sample in the batch
    err_l1_mean, err_l2_mean = jax.lax.fori_loop(
      body_fun=get_direct_errors,
      lower=0,
      upper=num_lead_times,
      init_val=(
        jnp.zeros(shape=(batch_size_per_device,)),
        jnp.zeros(shape=(batch_size_per_device,)),
      )
    )

    return err_l1_mean, err_l2_mean

  def evaluate(
    state: TrainState, batches: Iterable[Tuple[Array, Array]]) -> EvalMetrics:
    """Evaluates the model on a dataset based on multiple trajectory lengths."""

    error_ar_l1_per_var = []
    error_ar_l2_per_var = []
    error_ar_l1 = []
    error_ar_l2 = []
    error_dr_l1 = []
    error_dr_l2 = []

    for batch in batches:
      # Unwrap the batch
      batch = jax.tree_map(jax.device_put, batch)  # Transfer to device memory
      trajs, specs = batch

      # Downsample the trajectories
      # -> [batch_size * jump_steps, num_times, ...]
      # NOTE: The last timestep is excluded to make the length of all the trajectories even
      trajs = jnp.concatenate(jnp.split(
          (trajs[:, :-1]
          .reshape(FLAGS.batch_size, (len_traj-1) // jump_steps, jump_steps, *num_grid_points, num_vars)
          .swapaxes(1, 2)
          .reshape(FLAGS.batch_size, (len_traj-1), *num_grid_points, num_vars)),
          jump_steps,
          axis=1),
        axis=0,
      )
      specs = (jnp.tile(specs, jump_steps)
        .reshape(FLAGS.batch_size, jump_steps, -1)
        .swapaxes(0, 1)
        .reshape(FLAGS.batch_size * jump_steps, -1)
      ) if _use_specs else None

      # Split the batch between devices
      # -> [NUM_DEVICES, batch_size_per_device, ...]
      trajs = jnp.concatenate(jnp.split(jnp.expand_dims(
        trajs, axis=0), NUM_DEVICES, axis=1), axis=0)
      specs = jnp.concatenate(jnp.split(jnp.expand_dims(
        specs, axis=0), NUM_DEVICES, axis=1), axis=0) if _use_specs else None

      # Evaluate direct prediction
      _error_dr_l1_batch, _error_dr_l2_batch = _evaluate_direct_prediction(
        state, trajs, specs,
      )
      # Re-arrange the sub-batches gotten from each device
      _error_dr_l1_batch = _error_dr_l1_batch.reshape(batch_size_per_device * NUM_DEVICES, 1)
      _error_dr_l2_batch = _error_dr_l2_batch.reshape(batch_size_per_device * NUM_DEVICES, 1)
      # Append the errors to the list
      error_dr_l1.append(_error_dr_l1_batch)
      error_dr_l2.append(_error_dr_l2_batch)


      # TODO: Add evaluation at the final time-step

      # Evaluate autoregressive prediction
      # Get the input/target time indices
      idx_time = np.arange(num_times)
      idx_inp = idx_time[:1]
      idx_tgt = idx_time[:]
      # Split the dataset along the time axis
      u_inp = trajs[:, :, idx_inp]
      u_tgt = trajs[:, :, idx_tgt]
      # Get predictions and target
      u_prd = _predict_trajectory_autoregressively(state, specs, u_inp, num_times)
      # Re-arrange the predictions gotten from each device
      u_prd = u_prd.reshape(batch_size_per_device * NUM_DEVICES, *u_prd.shape[2:])
      u_tgt = u_tgt.reshape(batch_size_per_device * NUM_DEVICES, *u_tgt.shape[2:])
      # Compute and store metrics
      error_ar_l1_per_var.append(rel_l1_error(u_prd, u_tgt))
      error_ar_l2_per_var.append(rel_l2_error(u_prd, u_tgt))

    # Aggregate over the batch dimension and compute norm per variable
    error_dr_l1 = jnp.median(jnp.concatenate(error_dr_l1), axis=0).item()
    error_dr_l2 = jnp.median(jnp.concatenate(error_dr_l2), axis=0).item()
    error_l1_per_var_agg = jnp.median(jnp.concatenate(error_ar_l1_per_var), axis=0)
    error_l2_per_var_agg = jnp.median(jnp.concatenate(error_ar_l2_per_var), axis=0)
    error_ar_l1 = jnp.sqrt(jnp.sum(jnp.power(error_l1_per_var_agg, 2))).item()
    error_ar_l2 = jnp.sqrt(jnp.sum(jnp.power(error_l2_per_var_agg, 2))).item()

    # Build the metrics object
    metrics = EvalMetrics(
      error_autoreg_l1=error_ar_l1,
      error_autoreg_l2=error_ar_l2,
      error_direct_l1=error_dr_l1,
      error_direct_l2=error_dr_l2,
    )

    return metrics

  # Evaluate before training
  metrics_trn = evaluate(
    state=state,
    batches=dataset.batches(mode='train', batch_size=FLAGS.batch_size),
  )
  metrics_val = evaluate(
    state=state,
    batches=dataset.batches(mode='valid', batch_size=FLAGS.batch_size),
  )

  # Report the initial evaluations
  time_tot_pre = time() - time_int_pre
  logging.info('\t'.join([
    f'DRCT: {direct_steps : 02d}',
    f'URLL: {unroll_steps : 02d}',
    f'EPCH: {epochs_before : 04d}/{FLAGS.epochs : 04d}',
    f'TIME: {time_tot_pre : 06.1f}s',
    f'LR: {state.opt_state.hyperparams["learning_rate"].item() : .2e}',
    f'RMSE: {0. : .2e}',
    f'GRAD: {0. : .2e}',
    f'L2-AR: {metrics_val.error_autoreg_l2 * 100 : .2f}%',
    f'L2-DR: {metrics_val.error_direct_l2 * 100 : .2f}%',
  ]))

  # Set up the checkpoint manager
  with disable_logging(level=logging.FATAL):
    (DIR / 'metrics').mkdir(exist_ok=True)
    checkpointer = orbax.checkpoint.PyTreeCheckpointer()
    checkpointer_options = orbax.checkpoint.CheckpointManagerOptions(
      max_to_keep=1,
      keep_period=None,
      best_fn=(lambda metrics: metrics['valid']['autoreg']['l2']),  # TODO: Get the final timestep instead
      best_mode='min',
      create=True,)
    checkpointer_save_args = orbax_utils.save_args_from_target(target={'state': state})
    checkpoint_manager = orbax.checkpoint.CheckpointManager(
      (DIR / 'checkpoints'), checkpointer, checkpointer_options)

  for epoch in range(1, epochs+1):
    # Store the initial time
    time_int = time()

    # Train one epoch
    subkey_0, subkey_1, key = jax.random.split(key, num=3)
    state, loss, grad = train_one_epoch(
      state=state,
      batches=dataset.batches(mode='train', batch_size=FLAGS.batch_size, key=subkey_0),
      key=subkey_1
    )

    # Evaluate
    metrics_trn = evaluate(
      state=state,
      batches=dataset.batches(mode='train', batch_size=FLAGS.batch_size),
    )
    metrics_val = evaluate(
      state=state,
      batches=dataset.batches(mode='valid', batch_size=FLAGS.batch_size),
    )

    # Log the results
    time_tot = time() - time_int
    logging.info('\t'.join([
      f'DRCT: {direct_steps : 02d}',
      f'URLL: {unroll_steps : 02d}',
      f'EPCH: {epochs_before + epoch : 04d}/{FLAGS.epochs : 04d}',
      f'TIME: {time_tot : 06.1f}s',
      f'LR: {state.opt_state.hyperparams["learning_rate"].item() : .2e}',
      f'RMSE: {np.sqrt(loss).item() : .2e}',
      f'GRAD: {grad.item() : .2e}',
      f'L2-AR: {metrics_val.error_autoreg_l2 * 100 : .2f}%',
      f'L2-DR: {metrics_val.error_direct_l2 * 100 : .2f}%',
    ]))

    with disable_logging(level=logging.FATAL):
      checkpoint_metrics = {
        'loss': loss.item(),
        'train': {
          'autoreg': {
            'l1': metrics_trn.error_autoreg_l1,
            'l2': metrics_trn.error_autoreg_l2,
          },
          'direct': {
            'l1': metrics_trn.error_direct_l1,
            'l2': metrics_trn.error_direct_l2,
          },
        },
        'valid': {
          'autoreg': {
            'l1': metrics_val.error_autoreg_l1,
            'l2': metrics_val.error_autoreg_l2,
          },
          'direct': {
            'l1': metrics_val.error_direct_l1,
            'l2': metrics_val.error_direct_l2,
          },
        },
      }
      # Store the state and the metrics
      step = epochs_before + epoch
      checkpoint_manager.save(
        step=step,
        items={'state': state,},
        metrics=checkpoint_metrics,
        save_kwargs={'save_args': checkpointer_save_args}
      )
      with open(DIR / 'metrics' / f'{str(step)}.json', 'w') as f:
        json.dump(checkpoint_metrics, f)

  return state

def get_model(model_configs: Mapping[str, Any]) -> AbstractOperator:

  model = GraphNeuralPDESolver(
    **model_configs,
  )

  return model

def main(argv):
  if len(argv) > 1:
    raise app.UsageError('Too many command-line arguments.')

  # Check the available devices
  with disable_logging():
    process_index = jax.process_index()
    process_count = jax.process_count()
    local_devices = jax.local_devices()
  logging.info('JAX host: %d / %d', process_index, process_count)
  logging.info('JAX local devices: %r', local_devices)
  # We only support single-host training.
  assert process_count == 1

  # Initialize the random key
  key = jax.random.PRNGKey(SEED)

  # Read the dataset
  experiment = FLAGS.experiment
  dataset = Dataset(
    key=key,
    dir='/'.join([FLAGS.datadir, (experiment + '.nc')]),
    n_train=FLAGS.n_train,
    n_valid=FLAGS.n_valid,
    n_test=FLAGS.n_test,
    cutoff=17,
    downsample_factor=2,
  )
  dataset.compute_stats(
    residual_steps=(FLAGS.direct_steps * FLAGS.jump_steps),
    skip_residual_steps=FLAGS.jump_steps,
  )

  # Read the checkpoint
  if FLAGS.params:
    DIR_OLD_EXPERIMENT = DIR_EXPERIMENTS / FLAGS.params
    orbax_checkpointer = orbax.checkpoint.PyTreeCheckpointer()
    step = orbax.checkpoint.CheckpointManager(DIR_OLD_EXPERIMENT / 'checkpoints', orbax_checkpointer).latest_step()
    ckpt = orbax_checkpointer.restore(directory=(DIR_OLD_EXPERIMENT / 'checkpoints' / str(step) / 'default'))
    state = ckpt['state']
    params = state['params']
    with open(DIR_OLD_EXPERIMENT / 'configs.json', 'rb') as f:
      model_kwargs = json.load(f)['model_configs']
  else:
    params = None
    model_kwargs = None

  # Get the model
  if not model_kwargs:
    model_kwargs = dict(
      num_outputs=dataset.sample[0].shape[-1],
      num_grid_nodes=dataset.sample[0].shape[2:4],
      num_mesh_nodes=(FLAGS.num_mesh_nodes, FLAGS.num_mesh_nodes),
      overlap_factor_grid2mesh=FLAGS.overlap_factor_grid2mesh,
      overlap_factor_mesh2grid=FLAGS.overlap_factor_mesh2grid,
      num_multimesh_levels=FLAGS.num_multimesh_levels,
      latent_size=FLAGS.latent_size,
      num_mlp_hidden_layers=FLAGS.num_mlp_hidden_layers,
      num_message_passing_steps=FLAGS.num_message_passing_steps,
    )
  model = get_model(model_kwargs)

  # Store the configurations
  DIR.mkdir()
  logging.info(f'Experiment stored in {DIR.relative_to(DIR_EXPERIMENTS).as_posix()}')
  flags = {f: FLAGS.get_flag_value(f, default=None) for f in FLAGS}
  with open(DIR / 'configs.json', 'w') as f:
    json.dump(fp=f,
      obj={'flags': flags, 'model_configs': model.configs},
      indent=2,
    )

  # Split the epochs
  epochs_u00 = int(FLAGS.epochs // (1 + .2 * FLAGS.unroll_steps))
  if FLAGS.unroll_steps:
    epochs_uxx = int((FLAGS.epochs - epochs_u00) // FLAGS.unroll_steps)
    epochs_uff = epochs_uxx + (FLAGS.epochs - epochs_u00) % FLAGS.unroll_steps
  # TRY: Allocate more epochs to the final direct_steps
  epochs_u00_dxx = epochs_u00 // FLAGS.direct_steps
  epochs_u00_dff = epochs_u00_dxx + epochs_u00 % FLAGS.direct_steps

  # Initialzize the model or use the loaded parameters
  if not params:
    subkey, key = jax.random.split(key)
    sample_traj, sample_spec = dataset.sample
    num_grid_points = sample_traj.shape[2:4]
    num_vars = dataset.sample[0].shape[-1]
    model_init_kwargs = dict(
      u_inp=jnp.ones(shape=(FLAGS.batch_size, 1, *num_grid_points, num_vars)),
      ndt=1.,
      specs=(jnp.ones_like(sample_spec).repeat(FLAGS.batch_size, axis=0)
        if (sample_spec is not None) else None),
    )
    variables = jax.jit(model.init)(subkey, **model_init_kwargs)
    params = variables['params']

  # Calculate the total number of parameters
  n_model_parameters = np.sum(
  jax.tree_util.tree_flatten(jax.tree_map(lambda x: np.prod(x.shape).item(), params))[0]).item()
  logging.info(f'Total number of trainable paramters: {n_model_parameters}')

  # Train the model without unrolling
  epochs_trained = 0
  num_batches = dataset.nums['train'] // FLAGS.batch_size
  lr = optax.cosine_decay_schedule(
    init_value=FLAGS.lr,
    decay_steps=(epochs_u00 * num_batches),
    alpha=FLAGS.lr_decay,
  ) if FLAGS.lr_decay else FLAGS.lr
  tx = optax.inject_hyperparams(optax.adamw)(learning_rate=lr, weight_decay=1e-8)
  state = TrainState.create(apply_fn=model.apply, params=params, tx=tx)
  for _d in range(1, FLAGS.direct_steps+1):
    key, subkey = jax.random.split(key)
    epochs = (epochs_u00_dff if (_d == FLAGS.direct_steps) else epochs_u00_dxx)
    state = train(
      key=subkey,
      model=model,
      state=state,
      dataset=dataset,
      jump_steps=FLAGS.jump_steps,
      direct_steps=_d,
      unroll_steps=0,
      epochs=epochs,
      epochs_before=epochs_trained,
    )
    epochs_trained += epochs

  # Train the model with unrolling
  lr = FLAGS.lr * FLAGS.lr_decay
  tx = optax.inject_hyperparams(optax.adamw)(learning_rate=lr, weight_decay=1e-8)
  state = TrainState.create(apply_fn=model.apply, params=state.params, tx=tx)
  for _u in range(1, FLAGS.unroll_steps+1):
    key, subkey = jax.random.split(key)
    epochs = (epochs_uff if (_u == FLAGS.unroll_steps) else epochs_uxx)
    state = train(
      key=subkey,
      model=model,
      state=state,
      dataset=dataset,
      jump_steps=FLAGS.jump_steps,
      direct_steps=FLAGS.direct_steps,
      unroll_steps=_u,
      epochs=epochs,
      epochs_before=epochs_trained,
    )
    epochs_trained += epochs

if __name__ == '__main__':
  logging.set_verbosity('info')
  app.run(main)
