"""A library of auxliary functions and classes."""

from typing import Sequence, Callable, Tuple
import functools

import jax
import jax.numpy as jnp
import jax.tree_util as tree
import flax.linen as nn


def concatenate_args(args, kwargs, axis: int = -1):
  combined_args = tree.tree_flatten(args)[0] + tree.tree_flatten(kwargs)[0]
  concat_args = jnp.concatenate(combined_args, axis=axis)
  return concat_args

class AugmentedMLP(nn.Module):
  """
  Multi-layer perceptron with optional layer norm and learned correction on the last layer.
  Activation is applied on all layers except the last one.
  Multiple inputs are concatenated before being fed to the MLP.
  """

  layer_sizes: Sequence[int]
  activation: Callable
  use_layer_norm: bool = False
  use_learned_correction: bool = False
  concatenate_axis: int = -1

  def setup(self):
    self.layers = [nn.Dense(features) for features in self.layer_sizes]
    self.layernorm = nn.LayerNorm(
      reduction_axes=-1,
      feature_axes=-1,
      use_scale=True,
      use_bias=True,
    ) if self.use_layer_norm else None
    self.correction = LearnedCorrection(
      latent_size=self.layer_sizes[-1],  # TRY: other sizes
      correction_size=1,  # TRY: self.layer_sizes[-1]
    ) if self.use_learned_correction else None

  def __call__(self, *args, c = None, **kwargs):
    x = concatenate_args(args=args, kwargs=kwargs, axis=self.concatenate_axis)
    for layer in self.layers[:-1]:
      x = layer(x)
      x = self.activation(x)
    x = self.layers[-1](x)
    if self.layernorm:
      x = self.layernorm(x)
    if self.correction:
      assert c is not None
      x = self.correction(c=c, x=x)
    else:
      assert c is None
    return x

class LearnedCorrection(nn.Module):
  """
  Learned correction layer is designed to be applied after a normalization layer.
  Based on an input (e.g., time delta), it shifts and scales the distribution of its input.
  correction_size must either be 1 or the same as one of the input dimensions (broadcastable).
  """

  latent_size: Sequence[int]
  correction_size: int = 1

  def setup(self):
    self.mlp_scale = nn.Sequential(
      layers=[
        nn.Dense(self.latent_size),
        nn.sigmoid,
        nn.Dense(self.correction_size)
      ])
    self.mlp_bias = nn.Sequential(
      layers=[
        nn.Dense(self.latent_size),
        nn.sigmoid,
        nn.Dense(self.correction_size, bias_init=nn.initializers.constant(1.))
      ])

  def __call__(self, c, x):
    scale = self.mlp_scale(c)
    bias = self.mlp_bias(c)

    return x * scale + bias
