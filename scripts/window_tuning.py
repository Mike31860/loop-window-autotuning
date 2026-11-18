#!/usr/bin/env python3
"""Discover eligible loop choices, then independently tune contiguous windows."""
from __future__ import annotations

import argparse
import contextlib
import copy
import csv
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import traceback

OPTIONS = ("induction", "parallelize-loops", "reduction", "loop_interchange")
LEVELS = {"induction": (0, 3), "parallelize-loops": (0, 1), "reduction": (0, 2)}
FIXED_OPTIONS = {"alias": 2, "privatize": 2, "tinline": 0}


def parse_procedure_config(path):
    """Select one explicitly terminated function; orders are discovered by Cetus."""
    function, loop_ids, ended = None, [], False
    for number, raw_line in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if ended:
            raise ValueError("Unexpected content after END at line %d" % number)
        if line.startswith("FUNCTION_NAME:"):
            if function is not None:
                raise ValueError("Window mode accepts exactly one function")
            function = line[len("FUNCTION_NAME:"):].strip()
            if not re.fullmatch(r"[A-Za-z_]\w*", function):
                raise ValueError("Invalid function name at line %d" % number)
        elif line.startswith("LOOP_ID:"):
            if function is None:
                raise ValueError("FUNCTION_NAME must precede LOOP_ID")
            match = re.fullmatch(r"LOOP_ID:([^;]+);DESIRED:(none|0|auto)", line)
            if not match:
                raise ValueError("Window mode discovers legal orders; use DESIRED:none or DESIRED:auto")
            loop_id = match.group(1)
            if not re.fullmatch(re.escape(function) + r"#\d+", loop_id):
                raise ValueError("Window members must be outermost loops of " + function)
            loop_ids.append(loop_id)
        elif line == "END":
            ended = True
        else:
            raise ValueError("Unrecognized configuration line %d: %s" % (number, line))
    if not ended or not function or not loop_ids:
        raise ValueError("Configuration requires FUNCTION_NAME, LOOP_ID entries, and END")
    indices = [int(loop_id.rsplit("#", 1)[1]) for loop_id in loop_ids]
    if indices != list(range(indices[0], indices[0] + len(indices))):
        raise ValueError("Loop IDs must be unique, ordered, and consecutive")
    return function, loop_ids


def generate_windows(loop_ids, size):
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= len(loop_ids):
        raise ValueError("Window size must be between 1 and the number of loop nests")
    if len(loop_ids) != len(set(loop_ids)):
        raise ValueError("Loop IDs must be unique")
    return [list(loop_ids[start:start + size]) for start in range(len(loop_ids) - size + 1)]


def validate_eligibility(loop_ids, discovery):
    """Validate compiler evidence rather than falling back to a fixed search space."""
    if not isinstance(discovery, dict):
        raise ValueError("Eligibility discovery must return a structured object")
    if discovery.get("status", "complete") != "complete":
        raise ValueError("Eligibility discovery did not complete")
    discovered = discovery.get("loops", {})
    if set(discovered) != set(loop_ids):
        raise ValueError("Eligibility must describe exactly the active window's loops")
    normalized = {}
    for loop_id in loop_ids:
        entry = discovered[loop_id]
        choices = entry.get("options", {})
        if set(choices) != set(OPTIONS):
            raise ValueError("Incomplete eligible options for " + loop_id)
        normalized[loop_id] = {}
        for option in OPTIONS:
            values = choices[option]
            if not isinstance(values, (list, tuple)) or not values or values[0] != 0:
                raise ValueError("Every option must include disabled/original choice 0 first")
            if any(isinstance(value, bool) or not isinstance(value, (int, str)) for value in values):
                raise ValueError("Eligibility choices must be integer levels or loop-order strings")
            if len(values) != len(set(values)):
                raise ValueError("Eligibility contains duplicate or invalid choices")
            if option in LEVELS:
                if any(value not in LEVELS[option] for value in values):
                    raise ValueError("Unsupported discovered option level: " + option)
            else:
                original = entry.get("original_order")
                if isinstance(original, str):
                    original = original.split("_")
                for order in values[1:]:
                    if not isinstance(order, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:_[A-Za-z_]\w*)+", order):
                        raise ValueError("Invalid discovered loop order")
                    indices = order.split("_")
                    if not isinstance(original, (list, tuple)) or not original or any(
                            not isinstance(index, str) for index in original) or len(original) != len(set(original)):
                        raise ValueError("Reorderable nests require an unambiguous original order")
                    if len(indices) != len(original) or set(indices) != set(original):
                        raise ValueError("Discovered order is not a permutation of the loop indices")
                    if indices == list(original):
                        raise ValueError("The original order must use choice 0, not a duplicate permutation")
            normalized[loop_id][option] = list(values)
    return normalized


def candidate_configurations(loop_ids, eligibility):
    """Enumerate only discovered choices; every variant owns its dictionaries."""
    per_loop = []
    for loop in loop_ids:
        options = eligibility[loop]
        per_loop.append([dict(zip(OPTIONS, values))
                         for values in itertools.product(*(options[name] for name in OPTIONS))])
    for combination in itertools.product(*per_loop):
        yield {loop: dict(settings) for loop, settings in zip(loop_ids, combination)}


def variant_count(loop_ids, eligibility):
    return math.prod(len(eligibility[loop][option]) for loop in loop_ids for option in OPTIONS)


def serial_configuration(loop_ids):
    return {loop: {option: 0 for option in OPTIONS} for loop in loop_ids}


def _valid_measurement(measurement):
    elapsed = measurement.get("window_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= 0:
        raise ValueError("Evaluator must return finite, positive window_seconds")
    return measurement


def select_best(records):
    valid = [record for record in records if record["status"] == "ok"]
    if not valid:
        raise ValueError("No valid configurations were measured")
    for record in valid:
        _valid_measurement(record["measurement"])
    # Equal timings retain the first program, including the serial reference.
    return min(valid, key=lambda record: record["measurement"]["window_seconds"])


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def _write_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value)
    temporary.replace(path)


def _format_loop_structure(roots):
    """Describe nesting without treating sequential siblings as nested loops."""
    if not isinstance(roots, list) or not roots:
        raise ValueError("Loop-order evidence must contain at least one root")

    def render(node):
        if not isinstance(node, dict) or not isinstance(node.get("index"), str) or not re.fullmatch(
                r"[A-Za-z_]\w*", node["index"]) or not isinstance(node.get("children"), list):
            raise ValueError("Invalid loop-order structure")
        children = [render(child) for child in node["children"]]
        if not children:
            return node["index"]
        suffix = children[0] if len(children) == 1 else "(" + "; ".join(children) + ")"
        return node["index"] + "_" + suffix

    orders = [render(root) for root in roots]
    return orders[0] if len(orders) == 1 else "(" + "; ".join(orders) + ")"


def _selected_loop_orders(result):
    """Use inline emitted evidence, with saved-artifact support for older runs."""
    winner = result["winner_validation"]
    inline = winner.get("loop_structures", {})
    evidence = {} if all(loop in inline for loop in result["loop_ids"]) else winner.get("transformations", {})
    if isinstance(evidence, (str, Path)):
        path = Path(evidence)
        evidence = json.loads(path.read_text()) if path.is_file() else {}
    source_orders = None
    orders = {}
    for loop_id in result["loop_ids"]:
        if loop_id in inline:
            orders[loop_id] = _format_loop_structure(inline[loop_id])
            continue
        entry = evidence.get(loop_id, {})
        if entry.get("emitted_loop_structure") is not None:
            orders[loop_id] = _format_loop_structure(entry["emitted_loop_structure"])
            continue
        if entry.get("emitted_order"):
            order = entry["emitted_order"]
            if not isinstance(order, list) or any(not isinstance(index, str) or not re.fullmatch(
                    r"[A-Za-z_]\w*", index) for index in order):
                raise ValueError("Invalid saved loop order for " + loop_id)
            orders[loop_id] = "_".join(order)
            continue
        source = winner.get("generated_c")
        if source and Path(source).is_file() and re.fullmatch(r"exact_rhs#\d+", loop_id):
            if source_orders is None:
                from window_bt import loop_order_structure, outer_loop_spans
                data = Path(source).read_bytes()
                expected_hash = winner.get("source_sha256")
                if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
                    raise ValueError("Saved generated source hash mismatch: " + str(source))
                text = data.decode()
                source_orders = {"exact_rhs#%d" % index: loop_order_structure(text[start:stop])
                                 for index, (start, stop) in enumerate(outer_loop_spans(text))}
            if loop_id in source_orders:
                orders[loop_id] = _format_loop_structure(source_orders[loop_id])
                continue
        # Older successful evaluations already verified explicit requested orders.
        requested = result["selected"]["configuration"][loop_id]["loop_interchange"]
        if requested not in (0, "0"):
            orders[loop_id] = str(requested)
            continue
        raise ValueError("Missing loop-order evidence for " + loop_id
                         + "; retain its transformation record or generated source")
    return orders


def format_window_result(result):
    """Present selected per-nest settings with their fresh window measurements."""
    selected = result["selected"]["configuration"]
    winner = _valid_measurement(result["winner_validation"])
    serial = _valid_measurement(result["serial_validation"])
    orders = _selected_loop_orders(result)
    lines = ["Window#%d:" % result["window_number"]]
    for loop_id in result["loop_ids"]:
        options = selected[loop_id]
        fields = [label + "=" + ("Off" if options[key] in (0, "0") else "On")
                  for label, key in (("Parallelization", "parallelize-loops"),
                                     ("Induction", "induction"), ("Reduction", "reduction"))]
        fields.append("LoopInterchange=" + orders[loop_id])
        lines.append("  %s : Opt : [%s]" % (loop_id, ", ".join(fields)))
    lines.extend([
        "  WindowExecutionTime = %.9f s" % winner["window_seconds"],
        "  SerialExecutionTime = %.9f s" % serial["window_seconds"],
        "  SpeedupVsSerial = %.6fx" % (serial["window_seconds"] / winner["window_seconds"]),
    ])
    if "samples" in winner:
        lines.append("  Samples = %d" % len(winner["samples"]))
    if "repetitions" in winner:
        lines.append("  Repetitions = %d" % winner["repetitions"])
    return "\n".join(lines)


def format_results_text(completed, status="complete"):
    """Format completed windows without renumbering or combining their timings."""
    header = (
        "Independent window tuning results\n"
        "Status = %s\n\n"
        "Times are fresh-validation medians of whole-window elapsed time, in seconds.\n"
        "Times accumulate all function calls specified by Repetitions in each application execution.\n"
        "SpeedupVsSerial = SerialExecutionTime / WindowExecutionTime; greater than 1 is faster.\n"
        "LoopInterchange shows the actual outer-to-inner order, including the original order when retained.\n"
        "Parentheses with semicolons list sibling loops in source order, e.g. k_j_(i_m; m).\n"
        "Each window starts independently from serial; window times must not be added.\n\n"
    ) % status
    body = "\n\n".join(format_window_result(result) for result in completed)
    return header + (body or "No completed windows.") + "\n"


def _append_jsonl(path, value):
    with Path(path).open("a") as stream:
        stream.write(json.dumps(value, allow_nan=False) + "\n")


def _event(directory, event, **values):
    _append_jsonl(Path(directory) / "events.jsonl", {"time_utc": _now(), "event": event, **values})


def _actual_count(measurement):
    value = measurement.get("actual_execution_count", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("actual_execution_count must be a nonnegative integer")
    return value


def _write_variant_table(path, history):
    with Path(path).open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["variant_id", "kind", "status", "decision", "window_seconds",
                         "cache_hit", "actual_executions", "executable_sha256", "configuration", "error"])
        for record in history:
            measurement = record.get("measurement", {})
            writer.writerow([record["id"], record["kind"], record["status"], record.get("decision", ""),
                             measurement.get("window_seconds", ""), measurement.get("cache_hit", False),
                             _actual_count(measurement), measurement.get("executable_sha256", ""),
                             json.dumps(record["configuration"], sort_keys=True), record.get("error", "")])


def _record_history(directory, history, record):
    history.append(record)
    _append_jsonl(directory / "history.jsonl", record)
    _write_json(directory / "history.json", history)


def search_window(loop_ids, evaluator, output_dir, candidate_error_type=(), window_number=1):
    """Discover, generate, and evaluate one independent window from serial source."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if any((directory / name).exists() for name in ("results.json", "history.json", "eligibility.json")):
        raise ValueError("Refusing to overwrite an existing window search: " + str(directory))
    try:
        return _search_window(loop_ids, evaluator, directory, candidate_error_type, window_number)
    except Exception as error:
        _write_json(directory / "failure.json", {"status": "failed", "time_utc": _now(),
                    "error_type": type(error).__name__, "error": str(error), "traceback": traceback.format_exc()})
        _event(directory, "window_failed", error=str(error))
        raise


def _search_window(loop_ids, evaluator, directory, candidate_error_type, window_number):
    started = time.monotonic()
    _event(directory, "discovery_started", loops=loop_ids)
    print("Window %d %s: discovering eligible options and legal orders" %
          (window_number, ", ".join(loop_ids)), flush=True)
    discovery = evaluator.discover(list(loop_ids), directory / "discovery")
    eligibility = validate_eligibility(loop_ids, discovery)
    _write_json(directory / "eligibility.json", discovery)
    total = variant_count(loop_ids, eligibility)
    _event(directory, "discovery_completed", variant_count=total, options=eligibility)
    print("Window %d: %d eligible variants" % (window_number, total), flush=True)

    baseline_configuration = serial_configuration(loop_ids)
    baseline = _valid_measurement(evaluator.evaluate(list(loop_ids), copy.deepcopy(baseline_configuration),
                    directory / "baseline/initial", serial=True, use_cache=False))
    history = []
    best = {"id": "serial_baseline", "kind": "serial", "status": "ok", "decision": "initial_reference",
            "configuration": baseline_configuration, "measurement": baseline}
    _record_history(directory, history, best)
    for index, configuration in enumerate(candidate_configurations(loop_ids, eligibility), 1):
        variant_id = "variant_%06d" % index
        artifact_dir = directory / "variants" / variant_id
        record = {"id": variant_id, "kind": "cetus", "configuration": configuration,
                  "artifact_directory": str(artifact_dir)}
        _event(directory, "variant_started", variant_id=variant_id, configuration=configuration)
        try:
            measurement = _valid_measurement(evaluator.evaluate(
                list(loop_ids), copy.deepcopy(configuration), artifact_dir, serial=False, use_cache=True))
            decision = "not_selected"
            if measurement["window_seconds"] < best["measurement"]["window_seconds"]:
                decision = "new_best"
            elif measurement.get("cache_hit", False):
                decision = "equivalent_program"
            record.update(status="ok", measurement=measurement, decision=decision)
            if decision == "new_best":
                best = record
            detail = "%.9f seconds%s" % (measurement["window_seconds"],
                                         " (reused identical program)" if measurement.get("cache_hit", False) else "")
        except candidate_error_type as error:
            record.update(status="rejected", decision="excluded", error_type=type(error).__name__, error=str(error))
            failed_evaluation = artifact_dir / "evaluation.json"
            if failed_evaluation.is_file():
                record["measurement"] = json.loads(failed_evaluation.read_text())
            detail = "rejected: " + (str(error).splitlines() or [type(error).__name__])[0]
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_json(artifact_dir / "variant.json", record)
        _record_history(directory, history, record)
        _event(directory, "variant_completed", variant_id=variant_id, status=record["status"],
               decision=record["decision"], actual_execution_count=_actual_count(record.get("measurement", {})))
        _write_json(directory / "progress.json", {"phase": "search", "completed_variants": index,
                    "total_variants": total, "selected_variant": best["id"],
                    "actual_execution_count": sum(_actual_count(r.get("measurement", {})) for r in history),
                    "elapsed_seconds": time.monotonic() - started})
        print("Window %d variant %d/%d: %s" % (window_number, index, total, detail), flush=True)

    selected = select_best(history)
    _event(directory, "fresh_validation_started", selected_variant=selected["id"])
    baseline_validation = _valid_measurement(evaluator.evaluate(list(loop_ids), serial_configuration(loop_ids),
                    directory / "baseline/validation", serial=True, use_cache=False))
    if selected["kind"] == "serial":
        winner_validation = baseline_validation
    else:
        winner_validation = _valid_measurement(evaluator.evaluate(list(loop_ids), copy.deepcopy(selected["configuration"]),
                    directory / "selected/validation", serial=False, use_cache=False))
    successful = [r for r in history if r["status"] == "ok"]
    validation_records = [baseline_validation]
    if winner_validation is not baseline_validation:
        validation_records.append(winner_validation)
    actual = sum(_actual_count(r.get("measurement", {})) for r in history)
    actual += sum(_actual_count(m) for m in validation_records)
    verified = sum(r.get("measurement", {}).get("verification_count", 0) for r in history)
    verified += sum(m.get("verification_count", 0) for m in validation_records)
    result = {
        "schema_version": 2, "status": "complete", "window_number": window_number,
        "loop_ids": list(loop_ids), "objective": "median directly measured whole-window elapsed time",
        "search": "exhaustive combinations of discovered eligible choices; independent serial baseline",
        "outside_window": "original serial baseline", "variant_count": total, "candidate_count": total,
        "valid_variant_count": sum(r["status"] == "ok" for r in history[1:]),
        "rejected_variant_count": sum(r["status"] == "rejected" for r in history[1:]),
        "unique_executable_count": len({r["measurement"]["executable_sha256"] for r in successful}),
        "cached_variant_count": sum(r.get("measurement", {}).get("cache_hit", False) for r in history[1:]),
        "actual_execution_count": actual, "verified_execution_count": verified,
        "eligible_options": eligibility, "eligibility_file": str(directory / "eligibility.json"),
        "fixed_cetus_options": FIXED_OPTIONS, "serial_baseline": baseline, "selected": selected,
        "serial_validation": baseline_validation, "winner_validation": winner_validation,
        "search_window_speedup": baseline["window_seconds"] / selected["measurement"]["window_seconds"],
        "validation_window_speedup": baseline_validation["window_seconds"] / winner_validation["window_seconds"],
        "history_file": str(directory / "history.json"), "history_jsonl": str(directory / "history.jsonl"),
        "elapsed_seconds": time.monotonic() - started,
        "performance_note": "Functional test; no combined cross-window configuration or speedup."
    }
    _write_variant_table(directory / "variants.csv", history)
    _write_json(directory / "selected/configuration.json", selected["configuration"])
    _write_json(directory / "results.json", result)
    _write_text(directory / "results.txt", format_results_text([result]))
    _write_json(directory / "progress.json", {"phase": "complete", "completed_variants": total,
                "total_variants": total, "selected_variant": selected["id"], "actual_execution_count": actual})
    _event(directory, "window_completed", selected_variant=selected["id"], actual_execution_count=actual)
    (directory / "README.txt").write_text(
        "Independent window %d: %s\n\n" % (window_number, ", ".join(loop_ids)) +
        "eligibility.json and discovery/: compiler evidence and legal orders\n"
        "baseline/: direct serial reference and fresh validation\n"
        "variants/: every requested configuration, generated code, executable and logs\n"
        "history.json / history.jsonl / variants.csv: evaluations, equivalence and decisions\n"
        "selected/: chosen per-loop configuration and fresh validation (serial uses baseline/validation)\n"
        "results.txt: full CETUS loop IDs, selected settings, fresh window times and serial speedups\n"
        "results.json: complete window result; events.jsonl: stage and decision timeline\n"
        "All window times cover the full interval, including intervening statements.\n")
    print("Window %d complete: %s; %d variants, %d distinct executables, %d actual executions" %
          (window_number, selected["id"], total, result["unique_executable_count"], actual), flush=True)
    return result


def _positive_integer(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _command_info(command, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=15)
        return {"command": command, "returncode": result.returncode, "output": result.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": command, "error": str(error)}


def _provenance(tuning_root):
    files = [Path(__file__).resolve(), tuning_root / "scripts/window_bt.py", tuning_root / "run-windows.sh"]
    files.extend(sorted((tuning_root / "compiler-patches").glob("*")))
    return {
        "time_utc": _now(), "python": sys.version, "python_executable": sys.executable,
        "platform": platform.uname()._asdict(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "cpu_count": os.cpu_count(),
        "environment": {k: v for k, v in os.environ.items() if k.startswith(("SLURM_", "OMP_"))},
        "toolchain": {name: _command_info(command) for name, command in {
            "gcc": ["gcc", "--version"], "java": ["java", "-version"], "javac": ["javac", "-version"]}.items()},
        "git_commit": _command_info(["git", "rev-parse", "HEAD"], str(tuning_root)),
        "git_branch": _command_info(["git", "symbolic-ref", "--short", "HEAD"], str(tuning_root)),
        "git_status": _command_info(["git", "status", "--porcelain"], str(tuning_root)),
        "source_sha256": {str(path.relative_to(tuning_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in files if path.is_file()},
    }


class _Tee:
    def __init__(self, first, second):
        self.first, self.second = first, second

    def write(self, value):
        self.first.write(value)
        self.second.write(value)
        self.second.flush()
        return len(value)

    def flush(self):
        self.first.flush()
        self.second.flush()


def _run_summary(output, manifest, completed, status, error=None):
    summary = {"schema_version": 2, "status": status, "manifest": manifest, "windows": completed,
               "counts_scope": "completed_windows",
               "variant_count": sum(r["variant_count"] for r in completed),
               "actual_execution_count": sum(r["actual_execution_count"] for r in completed),
               "verified_execution_count": sum(r["verified_execution_count"] for r in completed)}
    if error is not None:
        summary["error"] = error
    _write_json(output / "results.json", summary)
    with (output / "summary.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["window", "loops", "variants", "distinct_executables", "cached_variants",
                         "actual_executions", "serial_validation_seconds", "selected_validation_seconds",
                         "validation_ratio", "selected_variant", "configuration"])
        for r in completed:
            writer.writerow([r["window_number"], ";".join(r["loop_ids"]), r["variant_count"],
                             r["unique_executable_count"], r["cached_variant_count"], r["actual_execution_count"],
                             r["serial_validation"]["window_seconds"], r["winner_validation"]["window_seconds"],
                             r["validation_window_speedup"], r["selected"]["id"],
                             json.dumps(r["selected"]["configuration"], sort_keys=True)])
    _write_text(output / "results.txt", format_results_text(completed, status))


def main(argv=None):
    tuning_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-root", type=Path, default=tuning_root.parent)
    parser.add_argument("--config", default="procedures_BT_exact")
    parser.add_argument("--window-size", type=_positive_integer, default=2)
    parser.add_argument("--window-index", type=_positive_integer, help="Run only this one-based window")
    parser.add_argument("--output", type=Path, help="New/empty run directory; otherwise create one under Runs/Window-Level-Tuning")
    parser.add_argument("--class", dest="class_name", default="S")
    parser.add_argument("--threads", type=_positive_integer, default=2)
    parser.add_argument("--samples", type=_positive_integer, default=3)
    parser.add_argument("--repetitions", type=_positive_integer, default=10,
                        help="Repeat exact_rhs initialization to resolve short window timings (default: 10)")
    parser.add_argument("--application-timeout", type=_positive_integer, default=120,
                        help="Maximum seconds for each full benchmark execution (default: 120)")
    args = parser.parse_args(argv)
    config = Path(args.config)
    if config.parent == Path("."):
        config = tuning_root / "configs" / (config.name if config.suffix == ".txt" else config.name + ".txt")
    function, loop_ids = parse_procedure_config(config)
    if function != "exact_rhs" or loop_ids != ["exact_rhs#%d" % index for index in range(5)]:
        raise ValueError("The initial BT backend requires exact_rhs#0 through exact_rhs#4 in source order")
    windows = generate_windows(loop_ids, args.window_size)
    selected_windows = list(enumerate(windows, 1))
    if args.window_index is not None:
        if args.window_index > len(windows):
            raise ValueError("--window-index exceeds the number of windows")
        selected_windows = [selected_windows[args.window_index - 1]]
    paper_root = args.paper_root.expanduser().resolve()
    if args.output is None:
        parent = paper_root / "Runs/Window-Level-Tuning"
        parent.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_BT_%s_%s_w%d_t%d" % (
            function, args.class_name, args.window_size, args.threads)
        suffix = 0
        while True:
            output = parent / (name + ("_%02d" % suffix if suffix else ""))
            try:
                output.mkdir()
                break
            except FileExistsError:
                suffix += 1
    else:
        output = args.output.expanduser().resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("--output must be new or empty: " + str(output))
        output.mkdir(parents=True, exist_ok=True)
    (output / "logs").mkdir()
    (output / "configuration.txt").write_text(config.read_text())
    manifest = {"schema_version": 2, "mode": "independent-eligible-window-tuning", "created_utc": _now(),
                "paper_root": str(paper_root), "configuration_file": str(config.resolve()), "function": function,
                "window_size": args.window_size, "windows_requested": [
                    {"number": number, "loops": members} for number, members in selected_windows],
                "class": args.class_name, "threads": args.threads, "samples": args.samples,
                "exact_rhs_repetitions": args.repetitions, "application_timeout_seconds": args.application_timeout,
                "fixed_cetus_options": FIXED_OPTIONS,
                "command_line": list(sys.argv[1:] if argv is None else argv), "provenance": _provenance(tuning_root)}
    _write_json(output / "manifest.json", manifest)
    (output / "README.txt").write_text(
        "Independent eligibility-guided window tuning\n\n"
        "manifest.json: input, allocation, affinity, toolchain and exact source provenance\n"
        "configuration.txt: selected function and loop IDs\n"
        "results.txt: selected per-loop settings, fresh window times and serial speedups\n"
        "results.json / summary.csv: independent window results and execution counts\n"
        "logs/driver.log: complete driver stdout and stderr\n"
        "window_NN/: eligibility evidence, baseline, variants, decisions, measurements and logs\n\n"
        "Each window starts from the same serial input. No selected settings or timing cache\n"
        "are carried between windows. Only distinct compiled programs are timed per window.\n"
        "Final selected and serial programs receive fresh measurements. Window timings must\n"
        "not be added together, and Class S smoke-test differences are not paper speedup claims.\n")
    completed = []
    print("Run directory: " + str(output), flush=True)
    with (output / "logs/driver.log").open("w") as log, contextlib.redirect_stdout(_Tee(sys.stdout, log)), \
            contextlib.redirect_stderr(_Tee(sys.stderr, log)):
        try:
            from window_bt import BTWindowEvaluator, CandidateError
            _run_summary(output, manifest, completed, "running")
            for number, members in selected_windows:
                window_dir = output / ("window_%02d" % number)
                evaluator = BTWindowEvaluator(paper_root, window_dir / "backend", class_name=args.class_name,
                                             threads=args.threads, samples=args.samples, repetitions=args.repetitions,
                                             application_timeout=args.application_timeout)
                completed.append(search_window(members, evaluator, window_dir, CandidateError, number))
                _run_summary(output, manifest, completed, "running")
            _run_summary(output, manifest, completed, "complete")
            print("INDEPENDENT_WINDOW_TUNING_PASSED: %d window(s)" % len(completed), flush=True)
        except Exception as error:
            _run_summary(output, manifest, completed, "failed", {
                "type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()})
            raise
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError) as error:
        print("Window tuning failed: " + str(error), file=sys.stderr)
        sys.exit(1)
