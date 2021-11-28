#!/bin/bash
#SBATCH --account=def-patricia
#SBATCH --gres=gpu:v100l:1       # Request GPU "generic resources"
#SBATCH --array=0-7       # $SLURM_ARRAY_TASK_ID takes values from 0 to 7 inclusive
#SBATCH --cpus-per-task=6  # Cores proportional to GPUs: 6 on Cedar, 16 on Graham.
#SBATCH --mem=187G       # Memory proportional to GPUs: 32000 Cedar, 64000 Graham.
#SBATCH --time=0-12:00
#SBATCH --output=main_pl_%N-%j.out

module purge

module load python/3.7

module load cuda/11.2.2 cudnn/8.2.0

#export XLA_FLAGS=--xla_gpu_cuda_data_dir=$EBROOTCUDA
#export XLA_PYTHON_CLIENT_PREALLOCATE=false


#virtualenv --no-download $SLURM_TMPDIR/env
source $SCRATCH/dtp_env/bin/activate

export NCCL_BLOCKING_WAIT=1 #Pytorch Lightning uses the NCCL backend for inter-GPU communication by default. Set this variable to avoid timeout errors.


#wandb login #179570568b97364ce6ce1d8517887d86828c3938 --relogin
wandb online

python /home/spinney/project/spinney/projet_Yoshua_Blake/main_pl.py dtp --array $SLURM_ARRAY_TASK_ID --running_sweep True
