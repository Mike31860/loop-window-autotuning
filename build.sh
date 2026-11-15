#!/usr/bin/env bash
set -euo pipefail
tuning_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/paper2026-tuner.XXXXXX")"
trap 'rm -rf -- "$build_dir"' EXIT
mkdir -p "$build_dir/classes"
javac -d "$build_dir/classes" -cp "$tuning_dir/java" "$tuning_dir"/java/app/*.java "$tuning_dir"/java/Entities/*.java
jar cfe "$build_dir/tuningExec.jar" app.Main -C "$build_dir/classes" .
mv -- "$build_dir/tuningExec.jar" "$tuning_dir/tuningExec.jar"
printf 'Built %s\n' "$tuning_dir/tuningExec.jar"
