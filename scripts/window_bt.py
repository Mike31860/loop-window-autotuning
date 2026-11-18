"""Isolated, correctness-checked NAS BT evaluator for independent loop windows.

Only exact_rhs is transformed. Every candidate uses the same serial source
snapshot and the same other translation-unit objects. Timing surrounds the
complete contiguous window; it is never reconstructed from loop timings.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone


TIMER = "TimeInstrument_Tuning_section_exact_rhs_S0"
BT_SOURCES = (
    "bt.c", "initialize.c", "exact_solution.c", "exact_rhs.c",
    "set_constants.c", "adi.c", "rhs.c", "x_solve.c", "y_solve.c",
    "z_solve.c", "add.c", "error.c", "verify.c", "solve_subs.c",
)
COMMON_SOURCES = ("print_results.c", "c_timers.c", "wtime.c")
CFLAGS = ("-O3", "-fopenmp", "-mcmodel=large")
CHOICES = {"induction": (0, 3), "parallelize-loops": (0, 1), "reduction": (0, 2)}
GLOBAL_FLAGS = (
    "-tinline=0", "-teliminate-branch=0", "-induction=0", "-privatize=0",
    "-reduction=0", "-parallelize-loops=0", "-loop_interchange=0",
    "-alias=2", "-ddt=2", "-ompGen=1", "-profitable-omp=0",
)
_LEXICAL = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)


class CandidateError(RuntimeError):
    """An evaluated candidate failed compilation, verification or scope checks."""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _mask(text):
    return _LEXICAL.sub(lambda m: "".join("\n" if c == "\n" else " " for c in m.group()), text)


def repair_legacy_timer_bound(header, driver):
    """Exclude an unused trailing timer from BT's inclusive named-timer report."""
    definitions = {}
    for match in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)[ \t]+([^\n]+)$", _mask(header), re.M):
        definitions.setdefault(match.group(1), []).append(match.group(2).strip())

    def timer_id(name):
        values = definitions.get(name, [])
        if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
            raise CandidateError("Cannot resolve legacy timer constant: " + name)
        return int(values[0])

    # Retain strings while masking comments so commented-out names cannot make
    # an uninitialized reporting slot appear safe.
    driver_code = _LEXICAL.sub(
        lambda m: _mask(m.group()) if m.group().startswith(("/*", "//")) else m.group(), driver)
    assignments = re.findall(r'\bt_names\s*\[([^\]]+)\]\s*=\s*("(?:\\.|[^"\\])*")\s*;', driver_code)
    assignment_count = len(re.findall(r"\bt_names\s*\[[^\]]+\]\s*=(?!=)", _mask(driver)))
    if not assignments or len(assignments) != assignment_count:
        raise CandidateError("Cannot establish initialized legacy timer names")
    named_ids = [timer_id(name.strip()) for name, _ in assignments]
    last = max(named_ids)
    if last >= 63 or sorted(named_ids) != list(range(1, last + 1)):
        raise CandidateError("Legacy timer names must initialize every slot from 1 through t_last")
    declared_ids = {timer_id(name) for name in definitions
                    if (name.startswith("t_") and name != "t_last") or
                    (name.startswith("TimeInstrument_Tuning_section_") and name != TIMER)}
    if declared_ids != set(named_ids):
        raise CandidateError("Legacy timer constants and initialized names disagree")
    previous = timer_id("t_last")
    if previous not in (last, last + 1):
        raise CandidateError("Unexpected legacy timer reporting bound")
    bound = re.search(r"^[ \t]*#[ \t]*define[ \t]+t_last[ \t]+([0-9]+)", _mask(header), re.M)
    if bound is None:
        raise CandidateError("Cannot locate the legacy timer reporting bound")
    # Only the isolated header's bound changes; slot 63 is reported separately.
    return header[:bound.start(1)] + str(last) + header[bound.end(1):]


def _matching(text, start, opening, closing):
    depth = 0
    for pos in range(start, len(text)):
        if text[pos] == opening:
            depth += 1
        elif text[pos] == closing:
            depth -= 1
            if depth == 0:
                return pos + 1
    raise CandidateError("Unbalanced C delimiter while identifying exact_rhs loops")


def outer_loop_spans(text):
    """Locate the five braced outermost for statements without counting comments."""
    masked = _mask(text)
    function = re.search(r"\bvoid\s+exact_rhs\s*\([^)]*\)\s*\{", masked)
    if function is None:
        raise CandidateError("Cannot locate the exact_rhs function definition")
    begin = function.end() - 1
    end = _matching(masked, begin, "{", "}")
    spans = []
    depth = 1
    pos = begin + 1
    while pos < end - 1:
        match = re.match(r"for\b\s*\(", masked[pos:]) if depth == 1 else None
        if match and (pos == 0 or not (masked[pos - 1].isalnum() or masked[pos - 1] == "_")):
            paren = masked.index("(", pos, pos + match.end())
            body = _matching(masked, paren, "(", ")")
            while masked[body].isspace():
                body += 1
            if masked[body] != "{":
                raise CandidateError("Only braced outermost exact_rhs loops are supported")
            stop = _matching(masked, body, "{", "}")
            spans.append((pos, stop))
            pos = stop
            continue
        depth += (masked[pos] == "{") - (masked[pos] == "}")
        pos += 1
    if len(spans) != 5:
        raise CandidateError("Expected five exact_rhs outermost loop nests, found " + str(len(spans)))
    return spans


def _normalized_loop(text):
    # Cetus metadata is not executable; preserve OpenMP directives in the hash.
    text = re.sub(r"^\s*#\s*pragma\s+(?:loop|cetus|event)\b[^\n]*", "", text, flags=re.M)
    text = _LEXICAL.sub(lambda m: " " if m.group().startswith(("/*", "//")) else m.group(), text)
    return re.findall(r"[A-Za-z_]\w*|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\S", text)


def perfect_nest_order(text):
    """Return outer-to-inner indices only for a single perfect, braced nest."""
    masked = _mask(text)
    masked = re.sub(r"^[ \t]*#[^\n]*", lambda m: " " * len(m.group()), masked, flags=re.M)
    position = len(masked) - len(masked.lstrip())
    order = []
    while True:
        match = re.match(r"for\b\s*\(\s*(?:int\s+)?([A-Za-z_]\w*)\s*=", masked[position:])
        if match is None:
            return None
        order.append(match.group(1))
        paren = masked.index("(", position)
        body = _matching(masked, paren, "(", ")")
        while body < len(masked) and masked[body].isspace():
            body += 1
        if body >= len(masked) or masked[body] != "{":
            return None
        stop = _matching(masked, body, "{", "}")
        child = body + 1
        while child < stop - 1 and masked[child].isspace():
            child += 1
        if not re.match(r"for\b\s*\(", masked[child:]):
            return None if re.search(r"\bfor\s*\(", masked[child:stop - 1]) else order
        child_paren = masked.index("(", child)
        child_body = _matching(masked, child_paren, "(", ")")
        while child_body < stop and masked[child_body].isspace():
            child_body += 1
        if child_body >= stop or masked[child_body] != "{":
            return None
        child_stop = _matching(masked, child_body, "{", "}")
        if masked[child_stop:stop - 1].strip():
            return None
        position = child


def loop_order_structure(text):
    """Return ordered for-loop trees, preserving repeated sibling nests.

    Each node has an index and ordered children. Unlike perfect_nest_order,
    this describes imperfect nests too; it does not establish interchange
    eligibility. Unsupported loop forms fail explicitly rather than producing
    a partial or guessed ordering.
    """
    masked = _mask(text)
    masked = re.sub(r"^[ \t]*#(?:[^\n]*\\\n)*[^\n]*",
                    lambda match: "".join("\n" if c == "\n" else " " for c in match.group()),
                    masked, flags=re.M)
    roots, stack = [], []
    for keyword in re.finditer(r"\b(?:for|while|do)\b", masked):
        start = keyword.start()
        if keyword.group() != "for":
            raise CandidateError("Loop-order reporting supports braced for loops only")
        header = re.match(r"for\s*\(", masked[start:])
        if header is None:
            raise CandidateError("Cannot identify the for-loop header for order reporting")
        paren = start + header.end() - 1
        header_stop = _matching(masked, paren, "(", ")")
        parts = masked[paren + 1:header_stop - 1].split(";")
        initializer = re.fullmatch(r"\s*(?:int\s+)?([A-Za-z_]\w*)\s*=(?!=)\s*(.+?)\s*",
                                   parts[0], flags=re.S)
        if len(parts) != 3 or initializer is None:
            raise CandidateError("Loop-order reporting requires a single initialized index")
        # A comma at the top level initializes another index; commas inside a
        # function call or array expression do not introduce a sibling index.
        depth = 0
        for character in initializer.group(2):
            if character in "([{":
                depth += 1
            elif character in ")]}":
                depth -= 1
            elif character == "," and depth == 0:
                raise CandidateError("Loop-order reporting requires a single initialized index")
        body = header_stop
        while body < len(masked) and masked[body].isspace():
            body += 1
        if body >= len(masked) or masked[body] != "{":
            raise CandidateError("Loop-order reporting supports braced for loops only")
        stop = _matching(masked, body, "{", "}")
        while stack and start >= stack[-1][1]:
            stack.pop()
        node = {"index": initializer.group(1), "children": []}
        if stack:
            parent_body, parent_stop, parent = stack[-1]
            if start <= parent_body or stop > parent_stop:
                raise CandidateError("Cannot establish loop nesting for order reporting")
            parent["children"].append(node)
        else:
            roots.append(node)
        stack.append((body, stop, node))
    if not roots:
        raise CandidateError("Cannot identify any for loops for order reporting")
    return roots


def parse_legal_orders(output, loop_id, original_order):
    """Parse only the requested loop's legacy Cetus legal-permutation report."""
    pattern = (r"^\s*LoopName:\s*" + re.escape(loop_id)
               + r"\s+Legal Loop Interchange Transformations:\s*\[([^\]]*)\]\s*$")
    matches = re.findall(pattern, output, re.M)
    if len(matches) > 1:
        raise CandidateError("Duplicate legal-order reports for " + loop_id)
    if not matches:
        reasons = [line.strip() for line in output.splitlines()
                   if loop_id in line and re.search(r"non-perfect|imperfect|imprefect|function call|complex bound|negative increment", line, re.I)]
        if original_order is not None and len(original_order) >= 2 and not reasons:
            raise CandidateError("Missing legal-order report or rejection reason for perfect nest " + loop_id)
        return [], reasons or ["The source is not a perfect nest with at least two loops"]
    if original_order is None:
        raise CandidateError("Cetus reported permutations for a source nest not recognized as perfect: " + loop_id)
    orders = []
    for entry in matches[0].split(","):
        tokens = entry.strip().replace("_", " ").split()
        if not tokens:
            continue
        if len(tokens) != len(original_order) or sorted(tokens) != sorted(original_order):
            raise CandidateError("Malformed legal permutation for " + loop_id + ": " + entry)
        order = "_".join(tokens)
        if order not in orders:
            orders.append(order)
    if "_".join(original_order) not in orders:
        raise CandidateError("Cetus's legal permutations omit the original order for " + loop_id)
    return orders, []


def instrument_serial(text, indices):
    spans = outer_loop_spans(text)
    start, stop = spans[indices[0]][0], spans[indices[-1]][1]
    return (text[:start] + "if (timeron) timer_start(" + TIMER + ");\n  "
            + text[start:stop] + "\n  if (timeron) timer_stop(" + TIMER + ");"
            + text[stop:])


def parse_window_time(output):
    matches = re.findall(r"^\s*" + TIMER + r"\s*=\s*(\S+)\s*$", output, re.M)
    if len(matches) != 1:
        raise CandidateError("Expected exactly one whole-window timing; found " + str(len(matches)))
    try:
        value = float(matches[0])
    except ValueError as error:
        raise CandidateError("Malformed whole-window timing") from error
    if not math.isfinite(value) or value <= 0:
        raise CandidateError("Whole-window timing must be finite and positive")
    return value


def validate_window(loop_ids, configuration):
    if not loop_ids:
        raise ValueError("A window cannot be empty")
    indices = []
    for loop in loop_ids:
        match = re.fullmatch(r"exact_rhs#([0-4])", loop)
        if match is None:
            raise ValueError("This backend supports exact_rhs#0 through exact_rhs#4 only")
        indices.append(int(match.group(1)))
    if indices != list(range(indices[0], indices[0] + len(indices))):
        raise ValueError("Window loops must be distinct, contiguous and in source order")
    if set(configuration) != set(loop_ids):
        raise ValueError("Configurations must contain precisely the active window's loops")
    for loop, options in configuration.items():
        if set(options) not in (set(CHOICES), set(CHOICES) | {"loop_interchange"}):
            raise ValueError("Each loop needs induction, parallelize-loops, reduction, and optionally loop_interchange")
        for key, value in options.items():
            if key == "loop_interchange":
                if value in (0, "0") and not isinstance(value, bool):
                    continue
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]\w*(?:_[A-Za-z]\w*)+", value):
                    raise ValueError("Interchange must be 0 or an underscore-separated loop order")
                if len(value.split("_")) != len(set(value.split("_"))):
                    raise ValueError("An interchange order cannot repeat an index")
            elif value not in CHOICES[key] or isinstance(value, bool):
                raise ValueError("Unsupported setting for " + loop + ": " + key)
    return indices


def cetus_flags(loop_ids, configuration):
    validate_window(loop_ids, configuration)
    loops = []
    for loop in loop_ids:
        settings = dict(configuration[loop], loop_interchange=configuration[loop].get("loop_interchange", 0))
        options = ("alias=2,tinline=0,induction={induction},privatize=2,"
                   "parallelize-loops={parallelize-loops},reduction={reduction},"
                   "loop_interchange={loop_interchange}").format(**settings)
        loops.append(loop + "%" + options)
    # The legacy parser accepts this bracketless form without leaving trailing
    # whitespace on the final option value (which otherwise changes '0').
    payload = "-Tuning_Analysis=" + str(len(loop_ids)) + ";exact_rhs>S0<" + "&".join(loops)
    return list(GLOBAL_FLAGS) + [payload]


class BTWindowEvaluator:
    def __init__(self, paper_root, output_dir, class_name="S", threads=2, samples=3, repetitions=1000,
                 application_timeout=120):
        self.paper_root = Path(paper_root).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        if class_name not in ("S", "W", "A", "B", "C"):
            raise ValueError("Unsupported NAS BT class")
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in (threads, samples, repetitions)):
            raise ValueError("Threads, samples and repetitions must be positive integers")
        if isinstance(application_timeout, bool) or not isinstance(application_timeout, int) or application_timeout < 1:
            raise ValueError("Application timeout must be a positive integer in seconds")
        self.class_name, self.threads = class_name, threads
        self.samples, self.repetitions = samples, repetitions
        self.application_timeout = application_timeout
        self.cetus = self.paper_root / "CETUS/cetus-tuning/bin/cetus"
        self.snapshot = self.output_dir / "serial_snapshot"
        self.objects = self.output_dir / "serial_objects"
        self.references = {}
        self.measurement_cache = {}
        self.discoveries = {}
        self.command_counts = {"total": 0, "transform": 0, "compile": 0, "link": 0,
                               "application": 0, "discovery": 0}
        self.env = dict(os.environ, OMP_NUM_THREADS=str(threads), OMP_DYNAMIC="FALSE",
                        OMP_PROC_BIND="close", OMP_PLACES="cores")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.snapshot.exists() or self.objects.exists():
            raise RuntimeError("Use a fresh output directory; a serial snapshot already exists")
        if not self.cetus.is_file():
            raise RuntimeError("Missing Cetus executable: " + str(self.cetus))
        try:
            self._prepare()
        except (CandidateError, OSError) as error:
            raise RuntimeError("Could not prepare the serial BT fixture: " + str(error)) from error

    def _run(self, command, cwd, log_prefix, timeout=300):
        prefix = Path(log_prefix)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        record = {"command": [str(arg) for arg in command], "cwd": str(cwd),
                  "timeout_seconds": timeout,
                  "started_utc": datetime.now(timezone.utc).isoformat()}
        started = time.monotonic()
        self.command_counts["total"] += 1
        category = next((key for key in ("application", "transform", "compile", "link", "discovery")
                         if prefix.name.startswith(key)), None)
        if category is not None:
            self.command_counts[category] += 1
        try:
            run = subprocess.run(record["command"], cwd=str(cwd), env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=timeout, check=False)
            output = run.stdout.decode("utf-8", errors="replace")
            record["returncode"] = run.returncode
        except subprocess.TimeoutExpired as error:
            data = error.stdout or b""
            output = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
            prefix.with_suffix(".out").write_text(output)
            record["timed_out"] = True
            record["elapsed_seconds"] = time.monotonic() - started
            write_json(prefix.with_suffix(".json"), record)
            raise CandidateError("Command timed out; see " + str(prefix)) from error
        prefix.with_suffix(".out").write_text(output)
        record["elapsed_seconds"] = time.monotonic() - started
        write_json(prefix.with_suffix(".json"), record)
        if run.returncode:
            raise CandidateError("Command failed (" + str(run.returncode) + "); see " + str(prefix))
        return output

    def _prepare(self):
        source_compiler = self.paper_root / "CETUS/cetus-tuning"
        compiler = self.output_dir / "window_compiler"
        helper = Path(__file__).resolve().parents[1] / "compiler-patches/build_window_compiler.py"
        if not helper.is_file():
            raise CandidateError("Missing required window compiler build helper")
        self._run([sys.executable, helper, source_compiler, compiler], self.output_dir,
                  self.output_dir / "prepare/window_compiler", timeout=300)
        self.cetus = compiler / "bin/cetus"
        suite = self.paper_root / "Benchmarks/SNU_NPB-1.0.3/NPB3.3-SER-C"
        originals = {}
        for directory, files in (("BT", [suite / "BT" / name for name in BT_SOURCES] + list((suite / "BT").glob("*.h"))),
                                 ("common", list((suite / "common").glob("*.h")) + [suite / "common" / n for n in COMMON_SOURCES]),
                                 ("sys", [suite / "sys/setparams.c"]),
                                 ("config", [suite / "config/make.def"])):
            destination = self.snapshot / directory
            destination.mkdir(parents=True, exist_ok=True)
            for source in files:
                if source.name == "npbparams.h":
                    continue
                shutil.copy2(str(source), str(destination / source.name))
                originals[str(source.relative_to(suite))] = sha256(source)
        app = self.snapshot / "BT"
        for name in BT_SOURCES:
            if re.search(r"^\s*#\s*pragma\s+omp\b", _LEXICAL.sub(lambda m: "" if m.group().startswith(("/*", "//")) else m.group(), (app / name).read_text()), re.M):
                raise CandidateError("Serial source already contains OpenMP directives: " + name)
        header = app / "header.h"
        if TIMER in header.read_text():
            raise CandidateError("The canonical BT header already defines the window timer")
        header.write_text(repair_legacy_timer_bound(header.read_text(), (app / "bt.c").read_text())
                          + "\n#define " + TIMER + " 63\n")
        timers = (self.snapshot / "common/c_timers.c").read_text()
        if not re.search(r"start\s*\[64\].*elapsed\s*\[64\]", timers):
            raise CandidateError("Cannot establish that timer slot 63 is available")
        exact = app / "exact_rhs.c"
        exact.write_text(exact.read_text().replace('#include "header.h"', '#include "header.h"\n#include "../common/timers.h"', 1))
        self.serial_text = exact.read_text()
        outer_loop_spans(self.serial_text)
        driver = app / "bt.c"
        text = driver.read_text()
        replacement = ("  /* Repeat the self-resetting forcing initialization for resolvable timings. */\n"
                       "  timer_clear(" + TIMER + ");\n"
                       "  for (int window_repeat = 0; window_repeat < " + str(self.repetitions) + "; ++window_repeat) {\n"
                       "    exact_rhs();\n  }\n"
                       '  printf("' + TIMER + ' = %.9f\\n", timer_read(' + TIMER + "));\n")
        text, count = re.subn(r"^\s*exact_rhs\s*\(\s*\)\s*;\s*$", lambda match: replacement, text, flags=re.M)
        if count != 1:
            raise CandidateError("Expected one exact_rhs initialization call in the serial BT driver")
        driver.write_text(text)
        self._run(["gcc", "-O2", "-o", self.snapshot / "sys/setparams", self.snapshot / "sys/setparams.c"],
                  self.snapshot / "sys", self.output_dir / "prepare/setparams_build")
        self._run([self.snapshot / "sys/setparams", "bt", self.class_name], app,
                  self.output_dir / "prepare/setparams_run")
        if not (app / "npbparams.h").is_file():
            raise CandidateError("setparams did not produce npbparams.h")
        self.objects.mkdir()
        self.object_paths = []
        for source in [app / name for name in BT_SOURCES if name != "exact_rhs.c"] + [self.snapshot / "common" / name for name in COMMON_SOURCES]:
            obj = self.objects / (source.stem + ".o")
            self._run(["gcc", *CFLAGS, "-I", app, "-I", self.snapshot / "common", "-c", source, "-o", obj],
                      app, self.output_dir / "prepare" / source.stem)
            self.object_paths.append(obj)
        self.snapshot_hashes = {str(p.relative_to(self.snapshot)): sha256(p)
                                for p in sorted(self.snapshot.rglob("*")) if p.suffix in (".c", ".h")}
        self.object_hashes = {str(p.name): sha256(p) for p in self.object_paths}
        self.compiler_hash = sha256(compiler / "lib/cetus.jar")
        probe = self.output_dir / "prepare/openmp_probe.c"
        probe.write_text('''#include <omp.h>
#include <stdio.h>
int main(void) {
  int team = 0;
  #pragma omp parallel shared(team)
  {
    #pragma omp single
    team = omp_get_num_threads();
  }
  printf("OPENMP_TEAM_SIZE=%d\\n", team);
  return 0;
}
''')
        probe_binary = probe.with_suffix("")
        self._run(["gcc", "-fopenmp", probe, "-o", probe_binary], self.output_dir,
                  self.output_dir / "prepare/openmp_probe_build")
        probe_output = self._run([probe_binary], self.output_dir,
                                 self.output_dir / "prepare/openmp_probe_run")
        team_sizes = re.findall(r"^OPENMP_TEAM_SIZE=(\d+)$", probe_output, re.M)
        if team_sizes != [str(self.threads)]:
            raise CandidateError("OpenMP did not provide the requested thread count: " + repr(team_sizes))
        self.runtime_threads = int(team_sizes[0])
        write_json(self.output_dir / "serial_snapshot.json", {
            "original_sources": originals, "fixture_sources": self.snapshot_hashes,
            "cached_serial_objects": self.object_hashes, "cetus_jar_sha256": self.compiler_hash,
            "class": self.class_name, "repetitions": self.repetitions, "timer_id": 63,
            "timer_label": TIMER, "cflags": list(CFLAGS), "threads": self.threads,
            "verified_openmp_team_size": self.runtime_threads,
            "process_cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "fixture_edits": ["Bound legacy timer reporting by the highest initialized name (t_last=22)",
                              "Reserve timer 63", "Include timer prototypes in exact_rhs",
                              "Repeat exact_rhs initialization and print the accumulated window time"],
            "environment": {k: self.env.get(k) for k in ("SLURM_JOB_ID", "SLURM_JOB_NODELIST", "SLURM_CPUS_PER_TASK", "OMP_NUM_THREADS", "OMP_DYNAMIC", "OMP_PROC_BIND", "OMP_PLACES")},
        })

    def _source_directory(self, destination):
        destination.mkdir(parents=True, exist_ok=False)
        app = destination / "source"
        app.mkdir()
        for source in (self.snapshot / "BT").glob("*.h"):
            shutil.copy2(str(source), str(app / source.name))
        (app / "exact_rhs.c").write_text(self.serial_text)
        (destination / "common").symlink_to(self.snapshot / "common", target_is_directory=True)
        return app

    def _transform(self, loop_ids, configuration, destination):
        app = self._source_directory(destination)
        flags = cetus_flags(loop_ids, configuration)
        write_json(destination / "flags.json", flags)
        self._run([self.cetus, *flags, "exact_rhs.c"], app, destination / "transform")
        generated = app / "cetus_output/exact_rhs.c"
        if not generated.is_file():
            raise CandidateError("Cetus did not emit exact_rhs.c")
        # Keep includes relative to the original source location, not cetus_output.
        final = app / "generated_exact_rhs.c"
        shutil.copy2(str(generated), str(final))
        return app, final, flags

    def _reference(self, loop_ids):
        key = tuple(loop_ids)
        if key not in self.references:
            options = {loop: {name: 0 for name in CHOICES} for loop in loop_ids}
            path = self.output_dir / "scope_references" / ("_".join(x.split("#")[1] for x in loop_ids))
            _, generated, _ = self._transform(loop_ids, options, path)
            self.references[key] = generated.read_text()
        return self.references[key]

    def _discovery_probe(self, loop_id, settings, destination):
        """Run one compiler query on fresh serial IR, without executing BT."""
        app = self._source_directory(destination)
        values = dict(flag.split("=", 1) for flag in GLOBAL_FLAGS)
        values.update({"-" + key: str(value) for key, value in settings.items()})
        flags = [key + "=" + value for key, value in values.items()]
        flags.append("-validity-opt=" + loop_id)
        write_json(destination / "flags.json", flags)
        output = self._run([self.cetus, *flags, "exact_rhs.c"], app,
                           destination / "discovery")
        generated = app / "cetus_output/exact_rhs.c"
        if not generated.is_file():
            raise CandidateError("Discovery did not emit its compiler artifact for " + loop_id)
        matches = re.findall(r"^\s*" + re.escape(loop_id) + r"\s*:\s*\[([^\]]*)\]\s*$", output, re.M)
        if len(matches) != 1:
            raise CandidateError("Expected exactly one applicability report for " + loop_id)
        record = {"loop_id": loop_id, "requested_probe": settings,
                  "reported_options": re.findall(r"-[A-Za-z_-]+", matches[0]),
                  "raw_applicability_report": matches[0], "compiler_log": str(destination / "discovery.out"),
                  "generated_c": str(generated), "source_sha256": sha256(generated),
                  "application_executions": 0, "fresh_serial_ir": True}
        write_json(destination / "probe.json", record)
        return generated.read_text(), output, record

    def discover(self, loop_ids, artifact_dir):
        """Discover eligible settings separately for every loop on fresh compiler IR."""
        validate_window(loop_ids, {loop: {option: 0 for option in CHOICES} for loop in loop_ids})
        destination = Path(artifact_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=False)
        original_spans = outer_loop_spans(self.serial_text)
        report = {
            "schema_version": 1, "status": "running", "loops": {},
            "context": {"input_sha256": hashlib.sha256(self.serial_text.encode()).hexdigest(),
                        "compiler_sha256": self.compiler_hash, "tinline": 0,
                        "alias": 2, "privatize": 2,
                        "strategy": "fresh serial source and compiler process for each loop and probe",
                        "application_executions": 0,
                        "conditional_eligibility": "Parallelization is probed with privatization and any recognized reduction prerequisite. Candidate transformations and permutations are checked again after application."}}
        for loop_id in loop_ids:
            index = int(loop_id.rsplit("#", 1)[1])
            loop_dir = destination / ("loop_%02d" % index)
            reference, _, reference_record = self._discovery_probe(loop_id, {}, loop_dir / "reference")
            ref_spans = outer_loop_spans(reference)
            ref_start, ref_stop = ref_spans[index]
            ref_tokens = _normalized_loop(reference[ref_start:ref_stop])
            original_order = perfect_nest_order(self.serial_text[slice(*original_spans[index])])
            entry = {"original_order": original_order,
                     "original_loop_structure": loop_order_structure(
                         self.serial_text[slice(*original_spans[index])]),
                     "options": {option: [0] for option in CHOICES},
                     "evidence": {"reference": reference_record}}
            for option, enabled, label in (("induction", 3, "Induction"),
                                            ("reduction", 2, "Reduction"),
                                            ("parallelize-loops", 1, "LoopParallelization")):
                settings = {option: enabled}
                if option == "parallelize-loops":
                    settings["privatize"] = 2
                    if 2 in entry["options"]["reduction"]:
                        settings["reduction"] = 2
                source, _, evidence = self._discovery_probe(loop_id, settings,
                                                            loop_dir / option)
                spans = outer_loop_spans(source)
                start, stop = spans[index]
                # Include the annotations immediately preceding the outermost for.
                function = re.search(r"\bvoid\s+exact_rhs\s*\([^)]*\)\s*\{", _mask(source))
                prefix = spans[index - 1][1] if index else function.end() - 1
                fragment = source[prefix:stop]
                reported = "-" + label in evidence["reported_options"]
                changed_body = _normalized_loop(source[start:stop]) != ref_tokens
                parallel_sites = len(re.findall(r"^\s*#\s*pragma\s+omp\s+parallel\s+for\b", fragment, re.M))
                reduction_sites = len(re.findall(r"^\s*#\s*pragma\s+(?:cetus|omp)\b[^\n]*\breduction\b", fragment, re.M))
                if option == "induction":
                    applicable = changed_body
                elif option == "reduction":
                    applicable = reported or reduction_sites > 0
                else:
                    applicable = parallel_sites > 0
                for other, (actual, expected) in enumerate(zip(spans, ref_spans)):
                    if other != index and _normalized_loop(source[slice(*actual)]) != _normalized_loop(reference[slice(*expected)]):
                        raise CandidateError("Eligibility probe changed another loop: " + loop_id)
                evidence.update(eligible=applicable, reported_eligible=reported,
                                loop_body_changed=changed_body, parallel_sites=parallel_sites,
                                reduction_annotation_sites=reduction_sites)
                if applicable:
                    entry["options"][option].append(enabled)
                entry["evidence"][option] = evidence
            _, output, evidence = self._discovery_probe(
                loop_id, {"loop_interchange": "validity"}, loop_dir / "loop_interchange")
            legal_orders, reasons = parse_legal_orders(output, loop_id, original_order)
            identity = "_".join(original_order) if original_order else None
            choices = [order for order in legal_orders if order != identity]
            entry["options"]["loop_interchange"] = [0] + choices
            evidence.update(eligible=bool(choices), legal_orders=legal_orders,
                            original_order=original_order, rejection_reasons=reasons)
            entry["evidence"]["loop_interchange"] = evidence
            self.discoveries[loop_id] = entry
            report["loops"][loop_id] = entry
            write_json(loop_dir / "eligibility.json", entry)
            write_json(destination / "eligibility.json", report)
        report["status"] = "complete"
        write_json(destination / "eligibility.json", report)
        return report

    def _transformation_evidence(self, text, reference, loop_ids, configuration):
        actual, expected = outer_loop_spans(text), outer_loop_spans(reference)
        evidence = {}
        for loop_id in loop_ids:
            index = int(loop_id.rsplit("#", 1)[1])
            start, stop = actual[index]
            ref_start, ref_stop = expected[index]
            prefix = actual[index - 1][1] if index else 0
            ref_prefix = expected[index - 1][1] if index else 0
            order = perfect_nest_order(text[start:stop])
            ref_order = perfect_nest_order(reference[ref_start:ref_stop])
            requested = configuration[loop_id].get("loop_interchange", 0)
            if requested not in (0, "0"):
                if order != str(requested).split("_"):
                    raise CandidateError("Requested interchange was not applied for " + loop_id
                                         + ": requested " + str(requested) + ", emitted " + str(order))
                if loop_id in self.discoveries and requested not in self.discoveries[loop_id]["options"]["loop_interchange"]:
                    raise CandidateError("Requested permutation was not discovered as legal for " + loop_id)
            elif order != ref_order:
                raise CandidateError("Loop order changed without an interchange request for " + loop_id)
            fragment = text[prefix:stop]
            evidence[loop_id] = {
                "requested": configuration[loop_id], "original_order": ref_order,
                "emitted_order": order, "interchange_applied": order != ref_order,
                "original_loop_structure": loop_order_structure(reference[ref_start:ref_stop]),
                "emitted_loop_structure": loop_order_structure(text[start:stop]),
                "parallel_directive_count": len(re.findall(r"^\s*#\s*pragma\s+omp\s+parallel\s+for\b", fragment, re.M)),
                "reduction_clause_count": len(re.findall(r"^\s*#\s*pragma\s+omp\b[^\n]*\breduction\b", fragment, re.M)),
                "normalized_code_changed": _normalized_loop(text[prefix:stop]) != _normalized_loop(reference[ref_prefix:ref_stop]),
            }
        return evidence

    def _scope_proof(self, text, reference, indices):
        actual = outer_loop_spans(text)
        expected = outer_loop_spans(reference)
        starts = list(re.finditer(r"\btimer_start\s*\(\s*" + TIMER + r"\s*\)", text))
        stops = list(re.finditer(r"\btimer_stop\s*\(\s*" + TIMER + r"\s*\)", text))
        if len(starts) != 1 or len(stops) != 1:
            raise CandidateError("Generated code must have exactly one start/stop pair for the window")
        function = re.search(r"\bvoid\s+exact_rhs\s*\([^)]*\)\s*\{", _mask(text))
        lower = actual[indices[0] - 1][1] if indices[0] else function.end() - 1
        upper = actual[indices[-1] + 1][0] if indices[-1] < 4 else len(text)
        if not (lower < starts[0].start() < actual[indices[0]][0]
                and actual[indices[-1]][1] < stops[0].start() < upper):
            raise CandidateError("The timer does not enclose exactly the requested contiguous window")
        for directive in re.finditer(r"^\s*#\s*pragma\s+omp\b", text, re.M):
            if not starts[0].start() < directive.start() < stops[0].end():
                raise CandidateError("OpenMP directive appears outside the active window")
        outside = {}
        for index, ((start, stop), (ref_start, ref_stop)) in enumerate(zip(actual, expected)):
            if index in indices:
                continue
            tokens = _normalized_loop(text[start:stop])
            ref_tokens = _normalized_loop(reference[ref_start:ref_stop])
            if tokens != ref_tokens:
                raise CandidateError("Cetus changed outside-window loop exact_rhs#" + str(index))
            outside["exact_rhs#" + str(index)] = hashlib.sha256(json.dumps(tokens).encode()).hexdigest()
        current = {str(p.relative_to(self.snapshot)): sha256(p)
                   for p in sorted(self.snapshot.rglob("*")) if p.suffix in (".c", ".h")}
        if current != self.snapshot_hashes or {p.name: sha256(p) for p in self.object_paths} != self.object_hashes:
            raise CandidateError("The serial input snapshot or outside-window objects changed")
        return {"passed": True, "outer_loop_count": 5, "timer_pairs": 1,
                "outside_loop_token_sha256": outside,
                "serial_snapshot_unchanged": True, "cached_serial_objects_unchanged": True,
                "outside_openmp_directives": 0, "reference": "direct serial source" if reference == self.serial_text else "Cetus with every tunable option disabled"}

    def evaluate(self, loop_ids, configuration, artifact_dir, serial=False, use_cache=True):
        indices = validate_window(loop_ids, configuration)
        if serial and any(value not in (0, "0") for values in configuration.values() for value in values.values()):
            raise ValueError("A serial reference must have every tunable option disabled")
        destination = Path(artifact_dir).expanduser().resolve()
        if destination == self.output_dir or destination in self.output_dir.parents:
            raise ValueError("Evaluation artifacts must have their own fresh directory")
        record = {"loop_ids": list(loop_ids), "configuration": configuration,
                  "serial": serial, "objective": "whole_window_elapsed_seconds",
                  "repetitions": self.repetitions, "threads": self.threads,
                  "application_timeout_seconds": self.application_timeout,
                  "actual_execution_count": 0, "execution_count": 0, "verification_count": 0,
                  "inherited_verification_count": 0, "cache_hit": False, "cached": False,
                  "measured": False, "evaluation_path": str(destination / "evaluation.json")}
        started = time.monotonic()
        try:
            if serial:
                app = self._source_directory(destination)
                generated = app / "generated_exact_rhs.c"
                generated.write_text(instrument_serial(self.serial_text, indices))
                flags = []
                write_json(destination / "flags.json", flags)
                reference = self.serial_text
            else:
                reference = self._reference(loop_ids)
                app, generated, flags = self._transform(loop_ids, configuration, destination)
            proof = self._scope_proof(generated.read_text(), reference, indices)
            write_json(destination / "scope_proof.json", proof)
            evidence = self._transformation_evidence(generated.read_text(),
                generated.read_text() if serial else reference, loop_ids, configuration)
            write_json(destination / "transformations.json", evidence)
            obj = destination / "exact_rhs.o"
            executable = destination / ("bt." + self.class_name + ".x")
            self._run(["gcc", *CFLAGS, "-I", app, "-I", self.snapshot / "common", "-c", generated, "-o", obj],
                      app, destination / "compile")
            self._run(["gcc", *CFLAGS, "-o", executable, *self.object_paths, obj, "-lm"],
                      app, destination / "link")
            (app / "timer.flag").touch()
            record.update(executable=str(executable),
                          generated_c=str(generated), flags=flags,
                          flags_path=str(destination / "flags.json"),
                          scope_proof=str(destination / "scope_proof.json"),
                          transformations=str(destination / "transformations.json"),
                          loop_structures={loop: evidence[loop]["emitted_loop_structure"] for loop in loop_ids},
                          source_sha256=sha256(generated), executable_sha256=sha256(executable),
                          input_sha256=hashlib.sha256(self.serial_text.encode()).hexdigest())
            cache_key = (tuple(loop_ids), record["executable_sha256"])
            cached = self.measurement_cache.get(cache_key) if use_cache else None
            if cached is not None:
                record.update(window_seconds=cached["window_seconds"], samples=list(cached["samples"]),
                              nas_time_step_seconds=list(cached["nas_time_step_seconds"]),
                              inherited_verification_count=cached["verification_count"],
                              cache_hit=True, cached=True, measured=False,
                              measurement_source=cached["evaluation_path"],
                              measurement_source_relative=os.path.relpath(cached["evaluation_path"], destination))
            else:
                values, program_values = [], []
                for sample in range(self.samples):
                    record["actual_execution_count"] += 1
                    record["execution_count"] += 1
                    output = self._run([executable], app, destination / ("application_" + str(sample + 1)),
                                       timeout=self.application_timeout)
                    if not re.search(r"^\s*Verification\s*=\s*SUCCESSFUL\s*$", output, re.M):
                        raise CandidateError("NAS numerical verification failed in sample " + str(sample + 1))
                    record["verification_count"] += 1
                    values.append(parse_window_time(output))
                    program = re.search(r"^\s*Time in seconds\s*=\s*([0-9.eE+-]+)\s*$", output, re.M)
                    program_values.append(float(program.group(1)) if program else None)
                record.update(window_seconds=statistics.median(values), samples=values,
                              nas_time_step_seconds=program_values, measured=True,
                              measurement_source=record["evaluation_path"], measurement_source_relative="evaluation.json")
            record.update(status="valid", elapsed_seconds=time.monotonic() - started,
                          cumulative_command_counts=dict(self.command_counts))
            write_json(destination / "evaluation.json", record)
            if cached is None:
                # A forced fresh validation must not replace the measurement used
                # to choose the winner. Initial serial references seed the cache.
                self.measurement_cache.setdefault(cache_key, dict(record))
            return record
        except CandidateError as error:
            destination.mkdir(parents=True, exist_ok=True)
            record.update(status="invalid", error=str(error), elapsed_seconds=time.monotonic() - started)
            write_json(destination / "evaluation.json", record)
            raise
