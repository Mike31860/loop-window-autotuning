#!/usr/bin/env bash
set -euo pipefail
tuning_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
paper_root="$(cd -- "$tuning_dir/.." && pwd)"
if [[ $# -eq 0 || "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'USAGE'
Usage: run.sh ALGORITHM BENCHMARK APPLICATION CLASS TUNING_FLAG LOOP_BEGINNING WINDOW CONFIG
Runs the existing Java tuner with eight arguments.
CONFIG is a basename from TuningSystem/configs, with optional .txt extension;
absolute paths and paths relative to the paper root are also accepted.
Example: run.sh 3 Nasa BT_EXACT_W1_L5 B Tuning N 1 procedures_BT_exact
Build first with TuningSystem/build.sh. Run tuning in a suitable compute allocation.
For independent searches scored by whole-window time, use run-windows.sh --help.
The legacy WINDOW argument above does not activate the new independent mode.
USAGE
    exit 0
fi
if [[ $# -ne 8 ]]; then
    printf 'Expected eight arguments. Use --help for usage.\n' >&2
    exit 2
fi
if [[ ! -f "$tuning_dir/tuningExec.jar" ]]; then
    printf 'Missing tuningExec.jar; run %s/build.sh first.\n' "$tuning_dir" >&2
    exit 1
fi
cd -- "$paper_root"
exec java "-Dtuning.root=$paper_root" -jar "$tuning_dir/tuningExec.jar" "$@"
