import jax.numpy as jnp
from graphneuralpdesolver.utils import Array, ScalarArray


def rel_l2_error(predictions: Array, labels: Array) -> Array:
    """
    Returns the relative L2-norm of the error per variable.
    ALl input shapes are [batch_size, num_times_output,
        num_grid_points_0, num_grid_points_1, num_outputs]
    Output shape is [batch_size, num_outputs].
    """

    # FIXME: Support 1D

    sum_err_per_var_squared = jnp.sum(jnp.power(predictions - labels, 2), axis=(1, 2, 3))
    sum_lab_per_var_squared = jnp.sum(jnp.power(labels, 2), axis=(1, 2, 3))
    rel_l2_err_per_var = jnp.sqrt(sum_err_per_var_squared / sum_lab_per_var_squared)

    return rel_l2_err_per_var

def rel_l1_error(predictions: Array, labels: Array) -> Array:
    """
    Returns the relative L2-norm of the error per variable.
    ALl input shapes are [batch_size, num_times_output,
        num_grid_points_0, num_grid_points_1, num_outputs]
    Output shape is [batch_size, num_outputs].
    """

    # FIXME: Support 1D

    sum_err_per_var_abs = jnp.sum(jnp.abs(predictions - labels), axis=(1, 2, 3))
    sum_lab_per_var_abs = jnp.sum(jnp.abs(labels), axis=(1, 2, 3))
    rel_l2_err_per_var = (sum_err_per_var_abs / sum_lab_per_var_abs)

    return rel_l2_err_per_var

def mse(predictions: Array, labels: Array) -> ScalarArray:
    """
    Returns the mean squared error.
    Input shapes are [batch_size, num_times_output, num_grid_points, num_outputs].
    Output shape is [1].
    """

    return jnp.mean(jnp.power(predictions - labels, 2))
