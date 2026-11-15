#!/usr/bin/env bash
set -euo pipefail
tuning_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$tuning_dir/scripts/window_tuning.py" "$@"
