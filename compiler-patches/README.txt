Isolated compiler fixes for eligible window tuning
=================================================

The helper requires the existing DARWIN Java 8/JDK environment. It produces an
isolated compiler from the deployed JAR and verifies three targeted fixes.

1. Per-invocation reduction strategy
-----------------------------------

The customized Driver invokes LoopParallelizationPass separately for each
target loop. Each invocation constructs ReductionTransform, including when
that loop's reduction analysis is disabled. The original ReductionTransform
reads its strategy once in a static initializer. If the first loop requests
reduction=0, it chooses the scalar fallback (1), and later reduction=2 loops
incorrectly keep that strategy. Conversely, the first loop's array strategy
can remain active during later passes.

The reduction patch moves strategy selection into the constructor and makes the field
an immutable instance field. It preserves the existing fallback for disabled
or invalid levels. Reduction analysis itself still decides which loops have
reduction annotations; this patch changes strategy ownership, not the set of
allowed optimization levels.

2. Targeted induction mode
--------------------------
The targeted IVSubstitution constructor previously left its mode field at zero.
It now delegates to the normal constructor, which initializes and validates the
requested induction level. Only that constructor is transplanted into the
deployed class; all other methods retain their original bytecode.

3. Bounded explicit loop permutation
-----------------------------------
The deployed interchange method repeatedly swapped the outermost loop header
with later headers until a target appeared. For four-level nests it could fail
to reach some requested permutations and loop indefinitely. The replacement
validates that the target is a complete permutation and places each requested
index using at most n-1 header swaps. Existing start() eligibility and legality
checks are retained. A postcondition confirms the requested order was realized.

Only interchangeLoop() is transplanted into the deployed LoopInterchange class.
In particular, the compiler's existing legal-permutation discovery and analysis
remain bytecode-identical in behavior; unrelated working-tree source changes
are not installed. The backend converts the existing legal-order report into
structured JSON and invokes discovery in fresh processes for each loop/pass.

Build an isolated compiler on DARWIN:

  python3 compiler-patches/build_window_compiler.py \
    /lustre/parot/MiguelRosas/Paper_2026_Tunning/CETUS/cetus-tuning \
    /absolute/path/to/new/run/compiler

The window backend invokes this helper automatically in each experiment's
output directory. The command above also permits a standalone regression
check. The generated wrapper accepts each argument separately (quoted "$@").
The shared compiler is never modified. Only ReductionTransform.class,
IVSubstitution.class, and LoopInterchange.class change. Every other JAR member
is checked for byte identity. MethodTransplant uses the ASM already bundled in
the JDK and checks that non-target methods retain their instruction/debug traces.
This preserves the deployed Driver and its earlier dependence-analysis fixes.

The Java regressions demonstrate the original reduction and induction failures,
and the original permutation nontermination (terminated by a five-second test
timeout). Tests against the delivered JAR check both optimization modes, all 24
four-level permutations, index/bound and body preservation, and rejection of
invalid requests. The full benchmark run separately validates numerical output
and transformation scope. Source/JAR hashes, exact patches, method-transplant
logs, and regression/build logs are retained in the output directory.

These fixes do not imply that a transformation is applicable or profitable for
exact_rhs. Eligibility discovery, actual emitted-code checks, numerical
verification, and window-time measurements remain distinct parts of the run.

Driver-to-C interchange integration check
----------------------------------------
Run the separate supported-bound fixture inside a compute allocation:

  python3 compiler-patches/verify_interchange_pipeline.py \
    /absolute/path/to/run/compiler /absolute/path/to/new/fixture-results

The fixture discovers legal orders for an independent 2x3x4x5 array kernel,
requires all 24 permutations, and sends each order through Tuning_Analysis.
It checks the emitted C iterator order, compiles and runs the generated code,
and verifies every array element and a serial-reference checksum. Exact
commands, compiler logs, generated C, native executables, execution output,
and a structured results.json are preserved. This complements the direct
Java permutation regression by exercising the deployed Driver protocol.

This fixture does not change BT eligibility. In the current compiler, the
perfect exact_rhs nests #0 and #4 fail its supported-bound classification
because their bounds combine grid_points array accesses and a constant inner
extent. Nests #1, #2, and #3 are non-perfect nests (and contain function calls).
Consequently, current exact_rhs discovery offers no nonidentity interchange
orders. The existing conservative eligibility and dependence checks remain
unchanged; unsupported bounds are not evidence that a reordering is illegal.
