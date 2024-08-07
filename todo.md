# Project Updates

- RIGNO v2.0 is ready: fully unstructured mesh now + discretization invariant + time-independent

(E401)
- Results with full grids can be reproduced with the new UNSTRUCTURED scheme
- Results with partial grids are comparable with structured

(E402)
- Wave equation with derivative stepping and known parameters works
- Wave equation with TAU_MAX works

(E403)
- Poisson equation results

## Paper details

- The whole unstructured mesh handling

- Lp metrics for unstructured meshes

- added support radius to the structural regional node features

- Time-independent datasets: no t, no tau

## SOME UNANSWERED QUESTIONS

- Instable with tau_max = 7

- Why NS-SVS + NS-Sines do not generalize on time??

- Is noise injection helpful for us?
    - It seems like that for every other architecture out there, noise injection
        reduces rollout errors. For us, at least training unrolling, did not bring
        any good, but maybe Gaussian noise injection does.
        On the other hand, without doing ANYTHING, we are able to control the noise,
        and even the self-induced rollout noises are damped (check rollout errors
        with high tau_max). Investigate if this is particular to our architecture.

- Does it really outperform FNO on 1D problems?

# NEXT STEPS

- Update slides

- Update test script
    - Build a graph for each discretization
    - Instead of resolutions, test with multiple space_subsample_factor's

# Future work

## Experiments

- Add more discretization invariance tests
    1. Shuffle nodes and edges
    2. Multiple random sub-samples of the full mesh
    3. Super-resolution and sub-resolution
    4. Trained on grid, validated on unstructured mesh
    5. Different x_inp and x_out (not supported currently)

- Trained with partial grid vs. trained with full grid

## Literature

- Quick overview of the recent literature
- Read gladstone2024mesh + li2020multipole and present them to Mishra
    - Read them with details and be careful !
    - The ideas are VERY similar and the performance is close
    - Maybe we need to benchmark gladstone2024mesh too
    - We should be careful with what we focus on:
        - a possibility is focusing on this new paradigm for down and up-scaling layers

## Architectural Experiments

- experiment with num_processor_repetitions !!
    - Check overfitting and overall performance
    - Compute parameter efficiency

## Uncertainty

- Add uncertainty to the errors
    * No need to retrain anything, just use edge masking
- Inspect uncertainty over rollout steps
- Inspect uncertainty with different tau
- Inspect uncertainty with extrapolation


## Data Augmentation

- Try shifting first (approved)
- Try repeating (physically incorrect)

## Benchmarks

- GNO, brandstetter, MeshGraphNets, (scOT), (FNO), (U-Net), (CNO), (GNOT)

- Compare model size
    - Number of parameters
    - FLOPs / MADD
    - Inference time (improve your benchmarking)



## General Boundary Conditions
- Extend for general boundary conditions (e.g., open, Robin)
- Impose Dirichlet boundary conditions differently

## Variable known parameters

- Extend autoregressive and unrollings to variable c

## Variable mesh

- Check RIGNO.variable_mesh and try it
- Extend autoregressive and unrollings to variable x

## Multi-level RIGNO:
- Make the encoder and decoder modular
    - Define graph downsampling and upsampling layers
    - You can give up the long-range connections in mesh to allow for unstructured "mesh"
    - Or improve the long-range connection strategy to allow for unstructured "mesh" (Check )
- Apply encoder with multiple mesh resolutions
    1. All from the grid
    2. Hierarchical, down-scale step by step
- Apply message-passing on all the meshes independently
- Decode from multiple mesh resolutions
    1. All directly to the grid
    2. Hierarchical, up-scale step by step

## Interpretation

- More systematic approaches
    - engineered input
        - Single Fourier mode
        - Single Riemann problems
    - Perturbed input
        - keep/remove Fourier modes
        - Perturb IC parameters

## Pre-training
- Read thesis

## Adaptive inference
- Adaptive time step
- Adaptive remeshing

## Sequential data

- Implement and try the LSTM idea from presentation-240315-updates

- Simpler than LSTM: Just sum the hidden mesh nodes before decoder

- Check LLM tasks (translation, prompt-based text generation, auto-completion)

- Check RNNs and transformers (the whole training scheme changes.. and becomes faster!!)


# Code and Performance


- Get rid of the NVIDIA driver compatiblity message: parallel compilation possibly faster

- Add setup.py with setuptools and read use cases:
    - %pip install --upgrade https://github.com/.../master.zip

- Write docstring and type annotations

- Try to understand why without preloading the dataset, loading batches takes longer with more GPUs or a larger model.

- Re-implement segment_sum to avoid constant folding
    - Not sure there is an easy solution
