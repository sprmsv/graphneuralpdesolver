import sys
from time import time
from typing import Tuple, Any, Mapping

from absl import app, flags, logging
import jax
import jax.numpy as jnp
import numpy as np
import optax
import flax.linen as nn
import flax.typing
from flax.training.train_state import TrainState

from graphneuralpdesolver.autoregressive import AutoregressivePredictor
from graphneuralpdesolver.dataset import read_datasets, shuffle_arrays
from graphneuralpdesolver.models.graphneuralpdesolver import GraphNeuralPDESolver
from graphneuralpdesolver.utils import disable_logging, Array
from graphneuralpdesolver.losses import loss_mse, error_rel_l2


SEED = 43

FLAGS = flags.FLAGS
flags.DEFINE_string(name='datadir', default=None, required=True,
  help='Path of the folder containing the datasets'
)
flags.DEFINE_integer(name='resolution', default=128, required=False,
  help='Resolution of the physical discretization'
)
flags.DEFINE_string(name='experiment', default=None, required=True,
  help='Name of the experiment: {"E1", "E2", "E3", "WE1", "WE2", "WE3"'
)
flags.DEFINE_integer(name='batch_size', default=2048, required=False,
  help='Size of a batch of training samples'
)
flags.DEFINE_float(name='lr', default=1e-03, required=False,
  help='Training learning rate'
)
flags.DEFINE_integer(name='epochs', default=20, required=False,
  help='Number of training epochs'
)
flags.DEFINE_integer(name='bundle_inputs', default=2, required=False,
  help='Number of the input time steps of the model'
)
flags.DEFINE_integer(name='bundle_outputs', default=1, required=False,
  help='Number of time steps as predicted by the model at each autoregressive step'
)
flags.DEFINE_integer(name='noise_steps', default=1, required=False,
  help='Number of autoregressive steps for getting a noisy input'
)
flags.DEFINE_bool(name='push_forward', default=False, required=False,
  help='If passed, the push-forward trick is applied'
)

PDETYPE = {
  'E1': 'CE',
  'E2': 'CE',
  'E3': 'CE',
  'WE1': 'WE',
  'WE2': 'WE',
  'WE3': 'WE',
}


def train(model: nn.Module, dataset_trn: Mapping[str, Array], dataset_val: dict[str, Array],
          epochs: int, key: flax.typing.PRNGKey, params: flax.typing.Collection = None):
  """Trains a model and returns the state."""

  num_samples_trn = dataset_trn['trajectories'].shape[0]
  num_times = dataset_trn['trajectories'].shape[1]
  num_times_input = FLAGS.bundle_inputs
  num_times_output = FLAGS.bundle_outputs
  batch_size = FLAGS.batch_size
  offset = FLAGS.noise_steps * num_times_output
  assert dataset_trn['trajectories'].shape[0] % batch_size == 0

  # Initialzize the model
  subkey, key = jax.random.split(key)
  sample_input_u = dataset_trn['trajectories'][:batch_size, :num_times_input]
  sample_input_specs = dataset_trn['specs'][:batch_size]
  variables = model.init(subkey, u=sample_input_u, specs=sample_input_specs)
  n_model_parameters = np.sum(
  jax.tree_util.tree_flatten(
    jax.tree_map(
      lambda x: np.prod(x.shape).item(),
      variables['params']
    ))[0]
  ).item()
  print(f'Total number of trainable paramters: {n_model_parameters}')

  tx = optax.adamw(learning_rate=1e-4, weight_decay=1e-8)
  state = TrainState.create(apply_fn=model.apply, params=variables['params'], tx=tx)
  predictor = AutoregressivePredictor(predictor=model)

  def compute_loss(params: flax.typing.Collection, specs: Array,
                   u_inp: Array, u_out: Array) -> Array:
    """Computes the prediction of the model and returns its loss."""

    variables = {'params': params}
    pred = predictor(
      variables=variables,
      u_inp=u_inp,
      specs=specs,
      num_steps=1,
    )
    return loss_mse(pred, u_out)

  def get_loss_and_grads(params: flax.typing.Collection, specs: Array,
                         u_inp: Array, u_out: Array) -> Tuple[Array, Any]:
    """
    Computes the loss and the gradients of the loss w.r.t the parameters.
    """

    # TODO: Optionally no push-forward

    # GET NOISY INPUT
    variables = {'params': params}
    rollout = predictor(
      variables=variables,
      u_inp=u_inp,
      specs=specs,
      num_steps=(offset // num_times_output)
    )
    u_inp_noisy = jnp.concatenate([u_inp, rollout], axis=1)[:, -num_times_input:]

    # COMPUTE AND APPLY GRADS
    loss, grads = jax.value_and_grad(compute_loss)(params, specs, u_inp_noisy, u_out)

    return loss, grads

  @jax.jit
  def update(state: TrainState, u_inp: Array, u_out: Array) -> Tuple[TrainState, Array]:
    """Returns updated variables and state."""

    loss, grads = get_loss_and_grads(
      params=state.params,
      specs=specs,
      u_inp=u_inp,
      u_out=u_out,
    )

    state = state.apply_gradients(grads=grads)

    return state, loss

  @jax.jit
  def compute_error_norm_per_var(state: TrainState, specs: Array, trajectory: Array) -> Array:
    """
    Predicts the trajectories autoregressively and returns L2-norm of the relative error.
    """

    input = trajectory[:, :num_times_input]
    label = trajectory[:, num_times_input:]

    variables = {'params': state.params}
    pred = predictor(
      variables=variables,
      u_inp=input,
      specs=specs,
      num_steps=((num_times - num_times_input) // num_times_output),
    )

    return error_rel_l2(pred, label)

  # EVALUATE BEFORE TRAINING
  error_val_per_var = compute_error_norm_per_var(state, dataset_val['specs'], dataset_val['trajectories'])
  error_val = jnp.sqrt(jnp.mean(jnp.power(error_val_per_var, 2))).item()
  print('\t'.join([
    f'EPCH: {0:04d}/{epochs:04d}',
    f'EVAL: {error_val:.2e}',
  ]))

  lead_times = jnp.arange(offset+num_times_input, num_times-num_times_output+1)

  for epoch in range(epochs):
    begin = time()

    # SHUFFLE TO GET DIFFERENT BATCHES
    subkey, key = jax.random.split(key)
    trajectories, specs = shuffle_arrays(subkey, [dataset_trn['trajectories'], dataset_trn['specs']])

    # SPLIT IN BATCHES
    num_batches = num_samples_trn // batch_size
    batches = (
      jnp.split(trajectories, num_batches),
      jnp.split(specs, num_batches)
    )

    # PERMUTE LEAD TIME RANDOMLY FOR EACH BATCH
    lead_times_per_batch: list[jnp.ndarray] = []
    for _ in range(num_batches):
      subkey, key = jax.random.split(key)
      lead_times_per_batch.append(jax.random.permutation(subkey, lead_times))

    loss_trn = []
    for idx_lead_time in range(len(lead_times)):
      _loss_trn = 0.
      # TODO: Concatenate multiple batches together to make the trainings even faster
      for idx_batch, batch in enumerate(zip(*batches)):
        lead_time = lead_times_per_batch[idx_batch][idx_lead_time].item()
        trajectory, specs = batch
        u_inp = trajectory[:, (lead_time-offset-num_times_input):(lead_time-offset)]
        u_out = trajectory[:, lead_time:(lead_time+num_times_output)]
        state, loss_batch = update(state, u_inp, u_out)
        _loss_trn += loss_batch.item() * batch_size / num_samples_trn
      loss_trn.append(_loss_trn)
      if idx_lead_time % 10 == 0:
        print('\t'.join([
          f'----',
          f'EPCH: {epoch+1:04d}/{epochs:04d}',
          f'PRGS: {(idx_lead_time+1) / len(lead_times) : 2.1%}',
          f'TIME: {time()-begin:06.1f}s',
          f'LOSS: {loss_trn[-1]:.2e}',
        ]))
        sys.stdout.flush()
    loss_trn_mean = np.mean(loss_trn)

    # Evaluation
    error_val_per_var = compute_error_norm_per_var(state, dataset_val['specs'], dataset_val['trajectories'])
    error_val = jnp.sqrt(jnp.mean(jnp.power(error_val_per_var, 2))).item()

    time_tot = time() - begin

    print('\t'.join([
      f'EPCH: {epoch+1:04d}/{epochs:04d}',
      f'TIME: {time_tot:06.1f}s',
      f'LOSS: {loss_trn_mean:.2e}',
      f'EVAL: {error_val:.2e}',
    ]))
    sys.stdout.flush()

  return state

def get_model(spatial: Mapping[str, Mapping[str, Any]],
              model_configs: Mapping[str, Any]) -> nn.Module:

  assert jax.devices()[0] in spatial['x']['grid'].devices()

  model = GraphNeuralPDESolver(
    x=spatial['x']['grid'],
    dx=spatial['x']['delta'],
    domain_x=spatial['x']['domain'],
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

  # Read the datasets
  experiment = FLAGS.experiment
  datasets = read_datasets(
    dir=FLAGS.datadir, pde_type=PDETYPE[experiment],
    experiment=experiment, nx=FLAGS.resolution)
  assert np.all(datasets['test']['x'] == datasets['valid']['x'])
  assert np.all(datasets['test']['x'] == datasets['train']['x'])
  spatial = {
    'x': {
      'grid': datasets['test']['x'],
      'delta': datasets['test']['dx'],
      'domain': datasets['test']['domain_x']
    }
  }
  datasets = jax.tree_map(jax.device_put, datasets)
  for space_dim in spatial.keys():
    spatial[space_dim]['grid'] = jax.device_put(spatial[space_dim]['grid'])

  # Get the model
  model = get_model(
    spatial=spatial,
    model_configs=dict(
      num_times_input=FLAGS.bundle_inputs,
      num_times_output=FLAGS.bundle_outputs,
      # TODO: Parameterize the model configs
      num_outputs=1,
      latent_size=128,
      num_mlp_hidden_layers=2,
      num_message_passing_steps=6,
      num_gridmesh_cover=4,
      num_gridmesh_overlap=2,
      num_multimesh_levels=5,
    )
  )

  # Check the array devices
  assert jax.devices()[0] in datasets['train']['trajectories'].devices()
  assert jax.devices()[0] in datasets['train']['specs'].devices()
  assert jax.devices()[0] in datasets['valid']['trajectories'].devices()
  assert jax.devices()[0] in datasets['valid']['specs'].devices()

  # Train the model
  key = jax.random.PRNGKey(SEED)
  state = train(
    model=model,
    dataset_trn=datasets['train'],
    dataset_val=datasets['valid'],
    epochs=FLAGS.epochs,
    key=key,
    params=None,
  )

  # TODO: Store the model / state / ModelConfigs

if __name__ == '__main__':
  logging.set_verbosity('info')
  app.run(main)
