# loop-window-autotuning

A Cetus-based autotuning framework that jointly selects per-loop optimizations within independent windows, minimizing whole-window execution time.

## Workflow

1. Discover applicable optimizations and legal loop reorderings for each nest using fresh compiler input.
2. Generate window variants from those choices, retaining separate settings for each nest.
3. Apply the transformations, verify the generated program, measure the complete window, and select the fastest valid configuration.

Each window starts independently from the serial baseline. Nests outside the active window remain serial, and selected settings are not propagated to other windows. Identical executables reuse measurements within their own window, with every variant and its measurement provenance retained. The serial reference and selected program receive fresh validation.

The current window backend supports **NAS BT's `exact_rhs` function**. Its five outermost nests produce four adjacent windows at width two. The Java tuner and other procedure configurations are retained as legacy functionality; they do not provide additional backends for the window driver.

## Requirements and layout

- Python 3.9 or newer.
- Java 8/JDK for the customized compiler and patch regressions.
- GCC with OpenMP; validated with GCC 14.2.0.
- The project's **customized Cetus distribution**, including its deployed JAR, companion libraries, and transform sources. Stock Cetus alone does not provide the required tuning and profiling protocol.
- The serial C NAS benchmark tree at the location shown below.

The full compiler and benchmark source trees are external dependencies. Arrange the workspace as follows:

```text
PAPER_ROOT/
├── TuningSystem/                    # this repository
├── CETUS/cetus-tuning/
│   ├── bin/cetus
│   ├── lib/cetus.jar
│   ├── lib/...
│   └── src/cetus/transforms/
│       ├── ReductionTransform.java
│       ├── IVSubstitution.java
│       └── LoopInterchange.java
└── Benchmarks/SNU_NPB-1.0.3/NPB3.3-SER-C/
    ├── BT/
    ├── common/
    ├── config/
    └── sys/
```

From the workspace root:

```bash
git clone https://github.com/Mike31860/loop-window-autotuning.git TuningSystem
```

The backend builds an isolated compiler with the required fixes for each window. See [compiler patch documentation](compiler-patches/README.txt) for the checks and regressions.

## Run BT `exact_rhs`

Inside a compute allocation, from `PAPER_ROOT`:

```bash
./TuningSystem/run-windows.sh \
  --paper-root "$PWD" --config procedures_BT_exact \
  --class S --window-size 2 --threads 16 --samples 3 --repetitions 10
```

On DARWIN, the supplied Slurm script requests `xlarge-mem`, one task, 16 CPUs, and 16 GB, and runs the four windows sequentially:

```bash
workgroup -g parot --command @ -- sbatch \
  TuningSystem/examples/bt_exact_rhs_windows.slurm \
  "$PWD" "$PWD/Runs/Window-Level-Tuning/my_run"
```

This script uses DARWIN's `vpkg_require` environment and the `parot` account. Adapt those settings for another cluster. The explicit output directory must be new or empty. Without `--output`, the driver creates a unique timestamped folder under `PAPER_ROOT/Runs/Window-Level-Tuning/`.

## Results and verification

Each run preserves parameters, toolchain and source provenance, per-nest eligibility evidence, configurations, generated code, executables, logs, correctness checks, timing samples, and selected settings. Start with `results.txt` for a readable report, `results.json` or `summary.csv` for structured data, and `logs/driver.log` for the run log. Each `window_NN/` contains its own discovery, baseline, variants, history, selected result, and `results.txt`.

The text report uses full CETUS loop IDs and lists the selected settings separately for each nest. For example, the first window of the validated BT run is:

```text
Window#1:
  exact_rhs#0 : Opt : [Parallelization=Off, Induction=Off, Reduction=Off, LoopInterchange=Off]
  exact_rhs#1 : Opt : [Parallelization=Off, Induction=Off, Reduction=Off, LoopInterchange=Off]
  WindowExecutionTime = 0.000701000 s
  SerialExecutionTime = 0.000701000 s
  SpeedupVsSerial = 1.000000x
  Samples = 3
  Repetitions = 10
```

Times are fresh-validation medians in seconds, accumulated across the stated number of function repetitions per application execution. Speedup is serial time divided by selected-window time; values above 1 indicate a speedup and values below 1 indicate a slowdown. `LoopInterchange=Off` retains the original order; an enabled permutation is shown explicitly, such as `LoopInterchange=m_i_j_k`. Induction and reduction display On/Off, while their exact numeric levels remain in the JSON results. A run-level report contains completed windows and records whether the run is still running, complete, or failed.

Run local regression tests from the repository directory:

```bash
python3 -m unittest discover -s tests -v
```

Audit a completed four-window, 16-thread Class S DARWIN test:

```bash
python3 tests/verify_darwin_windows.py /absolute/path/to/run_directory
```

The validated Class S test evaluated 16 variants across four windows, with 60 NAS-verified application executions. All four windows selected serial. This is a functional smoke test, not a performance evaluation.

For the current `exact_rhs` source, Cetus excludes induction and reduction choices that do not apply, and rejects interchange because of unsupported array bounds or imperfect nests. Explicit reordering support was separately verified by discovering, applying, compiling, and numerically checking all 24 orders of a supported four-level kernel.

See [the workflow documentation](WINDOW_TUNING.txt) for timing semantics, optimization choices, artifact layout, and limitations.
