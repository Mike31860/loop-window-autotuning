#!/bin/bash -l
#SBATCH --job-name=bt-window-eligible
#SBATCH --account=parot
#SBATCH --partition=xlarge-mem
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=16G
#SBATCH --time=01:30:00
#SBATCH --no-requeue

# Arguments: PAPER_ROOT NEW_RUN_DIRECTORY. Every window gets all 16 CPUs;
# the four searches run sequentially and each starts from serial sources.
set -eo pipefail
vpkg_require gcc/14.2.0
set -u
paper_root="${1:?Specify Paper_2026_Tunning root}"
run_root="${2:?Specify a new run directory}"
threads="${SLURM_CPUS_PER_TASK:-16}"
export OMP_NUM_THREADS="$threads" OMP_DYNAMIC=FALSE OMP_PROC_BIND=close OMP_PLACES=cores
export PYTHONUNBUFFERED=1
environment_file="$(mktemp "${TMPDIR:-/tmp}/bt-window-environment.XXXXXX")"
trap 'rm -f -- "$environment_file"' EXIT
{
    hostname
    date -u
    gcc --version
    python3 --version
    java -version
    env | sort | grep -E '^(SLURM_|OMP_)'
} > "$environment_file" 2>&1

set +e
srun --nodes=1 --ntasks=1 --cpus-per-task="$threads" --cpu-bind=cores \
    "$paper_root/TuningSystem/run-windows.sh" \
    --paper-root "$paper_root" --config procedures_BT_exact \
    --class S --window-size 2 --threads "$threads" --samples 3 --repetitions 10 \
    --output "$run_root"
status=$?
set -e
if [[ -d "$run_root" ]]; then
    mkdir -p "$run_root/logs"
    cp -- "$environment_file" "$run_root/logs/allocation_environment.txt"
    cp -- "$0" "$run_root/logs/submitted_job.slurm"
fi
exit "$status"
