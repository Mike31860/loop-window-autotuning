#!/usr/bin/env python3
"""Audit an eligibility-guided four-window BT run without executing the benchmark."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from window_bt import (TIMER, _mask, _matching, _normalized_loop, outer_loop_spans,
                       parse_window_time, perfect_nest_order, loop_order_structure)
from window_tuning import (candidate_configurations, serial_configuration,
                           validate_eligibility, variant_count)

PATCH_CLASSES = {"cetus/transforms/" + name + ".class" for name in
                 ("IVSubstitution", "LoopInterchange", "ReductionTransform")}
HARNESSES = {"InductionOptionRegression", "InterchangeRegression", "ReductionOptionRegression"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inside(path, directory):
    path, directory = Path(path).resolve(), Path(directory).resolve()
    require(path.is_relative_to(directory), "Artifact escapes its window: " + str(path))
    return path


def config_key(configuration):
    return json.dumps(configuration, sort_keys=True)


def audit_discovery(directory, loops, input_hash):
    discovery = read(directory / "eligibility.json")
    require(discovery == read(directory / "discovery/eligibility.json"), "Discovery copies disagree")
    eligibility = validate_eligibility(loops, discovery)
    context = discovery["context"]
    require(context["input_sha256"] == input_hash, "Discovery used a different serial input")
    require(context["application_executions"] == 0, "Discovery unexpectedly executed BT")
    require(not list((directory / "discovery").rglob("application_*.out")), "Discovery contains benchmark executions")
    for loop in loops:
        entry = discovery["loops"][loop]
        evidence = entry["evidence"]
        require(set(evidence) == {"reference", "induction", "reduction", "parallelize-loops", "loop_interchange"},
                "Incomplete discovery evidence for " + loop)
        for option, probe in evidence.items():
            generated = inside(probe["generated_c"], directory / "discovery")
            require(digest(generated) == probe["source_sha256"], "Discovery source changed")
            require(digest(generated.parents[1] / "exact_rhs.c") == input_hash, "Probe did not start from serial input")
            log = inside(probe["compiler_log"], directory / "discovery")
            require(log.is_file(), "Missing discovery log")
            command = read(log.with_suffix(".json"))
            require(command["returncode"] == 0 and not command.get("timed_out"), "Compiler discovery failed")
            require("-validity-opt=" + loop in command["command"], "Probe targeted the wrong loop")
            require(probe["fresh_serial_ir"] and probe["application_executions"] == 0, "Invalid discovery context")
        interchange = evidence["loop_interchange"]
        expected_orders = [order for order in interchange["legal_orders"]
                           if order != ("_".join(entry["original_order"]) if entry["original_order"] else None)]
        require(eligibility[loop]["loop_interchange"] == [0] + expected_orders, "Legal orders were dropped or invented")
        if not interchange["legal_orders"]:
            require(bool(interchange["rejection_reasons"]), "Missing reason for unavailable interchange")
        for option in ("induction", "reduction", "parallelize-loops"):
            require((len(eligibility[loop][option]) > 1) == evidence[option]["eligible"], "Eligibility choices disagree with evidence")
    return discovery, eligibility


def audit_scope_and_orders(measurement, directory, loops):
    text = Path(measurement["generated_c"]).read_text()
    spans = outer_loop_spans(text)
    indices = [int(loop.rsplit("#", 1)[1]) for loop in loops]
    reference_path = (directory / "backend/serial_snapshot/BT/exact_rhs.c" if measurement["serial"] else
                      directory / "backend/scope_references" / "_".join(str(i) for i in indices) / "source/generated_exact_rhs.c")
    reference = reference_path.read_text()
    reference_spans = outer_loop_spans(reference)
    masked = _mask(text)
    function = re.search(r"\bvoid\s+exact_rhs\s*\([^)]*\)\s*\{", masked)
    require(function is not None, "Missing exact_rhs definition")
    function_end = _matching(masked, function.end() - 1, "{", "}")
    starts = list(re.finditer(r"\btimer_start\s*\(\s*" + TIMER + r"\s*\)", masked))
    stops = list(re.finditer(r"\btimer_stop\s*\(\s*" + TIMER + r"\s*\)", masked))
    require(len(starts) == len(stops) == 1, "Wrong number of whole-window timers")
    lower = spans[indices[0] - 1][1] if indices[0] else function.end() - 1
    upper = spans[indices[-1] + 1][0] if indices[-1] < 4 else function_end
    require(lower < starts[0].start() < spans[indices[0]][0] and
            spans[indices[-1]][1] < stops[0].start() < upper, "Timer does not enclose the requested window")
    for match in re.finditer(r"^\s*#\s*pragma\s+omp\b", masked, re.M):
        require(starts[0].start() < match.start() < stops[0].end(), "OpenMP escaped the active window")
    proof = read(inside(measurement["scope_proof"], directory))
    require(proof["passed"] and proof["timer_pairs"] == 1 and proof["outer_loop_count"] == 5,
            "Scope proof did not pass")
    require(proof["outside_openmp_directives"] == 0 and proof["serial_snapshot_unchanged"] and
            proof["cached_serial_objects_unchanged"], "Invalid scope or source-isolation proof")
    outside = {"exact_rhs#%d" % i for i in range(5)} - set(loops)
    require(set(proof["outside_loop_token_sha256"]) == outside, "Outside-loop proof is incomplete")
    transformations = read(inside(measurement["transformations"], directory))
    require(set(transformations) == set(loops), "Incomplete transformation evidence")
    for i, (actual, original) in enumerate(zip(spans, reference_spans)):
        loop = "exact_rhs#%d" % i
        if loop in outside:
            tokens = _normalized_loop(text[slice(*actual)])
            require(tokens == _normalized_loop(reference[slice(*original)]), "Outside loop changed: " + loop)
            require(hashlib.sha256(json.dumps(tokens).encode()).hexdigest() == proof["outside_loop_token_sha256"][loop],
                    "Outside-loop hash is inconsistent")
        else:
            requested = measurement["configuration"][loop]["loop_interchange"]
            emitted = perfect_nest_order(text[slice(*actual)])
            original_order = perfect_nest_order(reference[slice(*original)])
            evidence = transformations[loop]
            require(evidence["requested"] == measurement["configuration"][loop], "Transformation flags disagree")
            require(evidence["emitted_order"] == emitted and evidence["original_order"] == original_order,
                    "Recorded loop order disagrees with generated C")
            require(emitted == (str(requested).split("_") if requested not in (0, "0") else original_order),
                    "Requested loop order was not actually emitted")
            require(evidence["interchange_applied"] == (emitted != original_order), "Incorrect interchange application report")
            if "emitted_loop_structure" in evidence or "loop_structures" in measurement:
                structure = loop_order_structure(text[slice(*actual)])
                require(evidence.get("emitted_loop_structure") == structure,
                        "Recorded emitted loop structure disagrees with generated C")
                require(evidence.get("original_loop_structure") == loop_order_structure(reference[slice(*original)]),
                        "Recorded original loop structure disagrees with the reference")
                require(measurement.get("loop_structures", {}).get(loop) == structure,
                        "Inline loop-order evidence disagrees with generated C")


def audit_measurement(measurement, directory, loops, manifest, input_hash, prior_measured,
                      force_fresh=False, expected_class="S"):
    own = inside(measurement["evaluation_path"], directory)
    require(read(own) == measurement, "Evaluation record differs from history/results")
    require(measurement["status"] == "valid" and measurement["loop_ids"] == loops, "Invalid evaluation")
    require(measurement["threads"] == manifest["threads"] and measurement["repetitions"] == manifest["exact_rhs_repetitions"],
            "Evaluation context differs from the run")
    require(measurement["input_sha256"] == input_hash, "Candidate did not start from the serial snapshot")
    executable = inside(measurement["executable"], directory)
    generated = inside(measurement["generated_c"], directory)
    require(digest(executable) == measurement["executable_sha256"], "Executable hash mismatch")
    require(digest(generated) == measurement["source_sha256"], "Generated-source hash mismatch")
    require(len(measurement["samples"]) == manifest["samples"], "Wrong timing sample count")
    require(all(math.isfinite(value) and value > 0 for value in measurement["samples"]), "Invalid timing sample")
    require(measurement["window_seconds"] == statistics.median(measurement["samples"]), "Wrong window objective")
    audit_scope_and_orders(measurement, directory, loops)
    source = inside(measurement["measurement_source"], directory)
    require((own.parent / measurement["measurement_source_relative"]).resolve() == source, "Measurement pointers disagree")
    sample_count = manifest["samples"]
    if measurement["cache_hit"]:
        require(not force_fresh, "Fresh validation incorrectly reused a cached timing")
        require(measurement["actual_execution_count"] == measurement["verification_count"] == 0 and
                not measurement["measured"], "Cached timing was counted as new execution")
        require(source in prior_measured, "Cache reused another window or a not-yet-measured program")
        original = prior_measured[source]
        require(original["executable_sha256"] == measurement["executable_sha256"], "Cache matched unequal programs")
        require(original["samples"] == measurement["samples"] and original["window_seconds"] == measurement["window_seconds"],
                "Cache changed the reused measurement")
        require(measurement["inherited_verification_count"] == original["verification_count"] == sample_count,
                "Cache lacks verified source executions")
        require(not list(own.parent.glob("application_*.out")), "Cached variant unexpectedly contains new application runs")
        return 0
    require(source == own and measurement["measured"], "Fresh measurement has an invalid source pointer")
    require(measurement["actual_execution_count"] == measurement["verification_count"] == sample_count and
            measurement["inherited_verification_count"] == 0, "Incorrect fresh execution counts")
    require(len(list(own.parent.glob("application_*.out"))) == sample_count, "Wrong raw application-output count")
    for sample, expected in enumerate(measurement["samples"], 1):
        output_path = own.parent / ("application_%d.out" % sample)
        output = output_path.read_text()
        require(re.search(r"^\s*Verification\s*=\s*SUCCESSFUL\s*$", output, re.M), "NAS verification failed")
        require(re.search(r"^\s*Class\s*=\s*" + re.escape(expected_class) + r"\s*$", output, re.M),
                "Application did not run BT Class " + expected_class)
        require(parse_window_time(output) == expected, "Stored timing differs from raw output")
        command = read(output_path.with_suffix(".json"))
        require(command["returncode"] == 0 and not command.get("timed_out"), "Application command failed")
        require(command["command"] == [str(executable)], "Output belongs to a different executable")
    prior_measured[own] = measurement
    return sample_count


def verify(root, expected_threads=16, expected_partition="xlarge-mem", expected_class="S"):
    require(expected_class in ("S", "W", "A", "B", "C"), "Unsupported NAS BT class")
    root = Path(root).resolve()
    report, manifest = read(root / "results.json"), read(root / "manifest.json")
    require(report["status"] == "complete" and report["manifest"] == manifest, "Run did not complete")
    require(manifest["mode"] == "independent-eligible-window-tuning" and manifest["class"] == expected_class and
            manifest["window_size"] == 2 and manifest["threads"] == expected_threads, "Unexpected run configuration")
    environment = manifest["provenance"]["environment"]
    require(environment.get("SLURM_JOB_PARTITION") == expected_partition, "Wrong Slurm partition")
    require(int(environment.get("SLURM_CPUS_PER_TASK", 0)) == expected_threads, "Wrong requested CPUs per task")
    affinity = manifest["provenance"].get("cpu_affinity")
    require(affinity is None or len(affinity) >= expected_threads, "Insufficient process CPU affinity")
    require([r["window_number"] for r in report["windows"]] == [1, 2, 3, 4], "Expected four independent windows")
    require([r["number"] for r in manifest["windows_requested"]] == [1, 2, 3, 4], "Window request manifest is incomplete")
    originals, inputs, compilers, rows = [], set(), set(), []
    total_runs, total_variants, previous_finished = 0, 0, None
    for result in report["windows"]:
        number = result["window_number"]
        directory = root / ("window_%02d" % number)
        require(read(directory / "results.json") == result, "Per-window result differs from aggregate")
        loops = ["exact_rhs#%d" % i for i in (number - 1, number)]
        require(result["status"] == "complete" and result["loop_ids"] == loops, "Wrong window membership")
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        started = [datetime.fromisoformat(e["time_utc"]) for e in events if e["event"] == "discovery_started"]
        finished = [datetime.fromisoformat(e["time_utc"]) for e in events if e["event"] == "window_completed"]
        require(len(started) == len(finished) == 1 and started[0] <= finished[0], "Invalid window event timeline")
        require(previous_finished is None or previous_finished <= started[0], "Window searches overlapped")
        previous_finished = finished[0]
        snapshot = read(directory / "backend/serial_snapshot.json")
        require(snapshot["class"] == expected_class and snapshot["threads"] == expected_threads and
                snapshot["verified_openmp_team_size"] == expected_threads,
                "Snapshot class or OpenMP team does not match the request")
        require(snapshot["repetitions"] == manifest["exact_rhs_repetitions"], "Repetition count changed")
        require(re.search(r"^OPENMP_TEAM_SIZE=%d$" % expected_threads,
                          (directory / "backend/prepare/openmp_probe_run.out").read_text(), re.M), "Missing runtime thread-count evidence")
        for path, expected in snapshot["fixture_sources"].items():
            require(digest(directory / "backend/serial_snapshot" / path) == expected, "Serial fixture changed")
        for path, expected in snapshot["cached_serial_objects"].items():
            require(digest(directory / "backend/serial_objects" / path) == expected, "Untuned object changed")
        input_hash = digest(directory / "backend/serial_snapshot/BT/exact_rhs.c")
        inputs.add(input_hash)
        originals.append(snapshot["original_sources"])
        discovery, eligibility = audit_discovery(directory, loops, input_hash)
        total = variant_count(loops, eligibility)
        require(result["eligible_options"] == eligibility and result["variant_count"] == result["candidate_count"] == total,
                "Variant count does not match discovered choices")
        history = read(directory / "history.json")
        require(history == [json.loads(line) for line in (directory / "history.jsonl").read_text().splitlines()], "History formats disagree")
        require(len(history) == total + 1 and history[0]["kind"] == "serial", "Missing baseline or variants")
        require(history[0]["configuration"] == serial_configuration(loops), "Window did not start from serial settings")
        require(result["valid_variant_count"] == total and result["rejected_variant_count"] == 0,
                "This full smoke test contains rejected variants")
        expected_configs = {config_key(c) for c in candidate_configurations(loops, eligibility)}
        require({config_key(r["configuration"]) for r in history[1:]} == expected_configs, "Variant grid is incomplete or contains ineligible choices")
        seen, window_runs, hashes = {}, 0, set()
        for record in history:
            require(record["status"] == "ok", "Invalid variant in successful history")
            measurement = record["measurement"]
            require(measurement["configuration"] == record["configuration"], "Applied configuration differs from variant")
            window_runs += audit_measurement(measurement, directory, loops, manifest, input_hash, seen,
                                             force_fresh=record["kind"] == "serial", expected_class=expected_class)
            hashes.add(measurement["executable_sha256"])
        require(result["serial_baseline"] == history[0]["measurement"], "Serial baseline differs from history")
        expected_best = min(history, key=lambda r: r["measurement"]["window_seconds"])
        require(result["selected"] == expected_best, "Selection did not minimize the measured window objective")
        validation = [result["serial_validation"]]
        if result["selected"]["kind"] == "serial":
            require(result["winner_validation"] == validation[0], "Selected serial reference lacks fresh validation")
        else:
            validation.append(result["winner_validation"])
        require(validation[0]["configuration"] == serial_configuration(loops) and validation[0]["serial"], "Invalid serial validation")
        require(result["winner_validation"]["configuration"] == result["selected"]["configuration"], "Winner replay used different settings")
        for measurement in validation:
            require(Path(measurement["evaluation_path"]).resolve() not in seen, "Validation reused a previous evaluation artifact")
            window_runs += audit_measurement(measurement, directory, loops, manifest, input_hash, seen,
                                             force_fresh=True, expected_class=expected_class)
        require(result["actual_execution_count"] == result["verified_execution_count"] == window_runs,
                "Actual versus inherited execution counts are incorrect")
        require(result["cached_variant_count"] == sum(r["measurement"]["cache_hit"] for r in history[1:]), "Wrong cache-hit count")
        require(result["unique_executable_count"] == len(hashes), "Wrong distinct-program count")
        compiler_root = directory / "backend/window_compiler"
        compiler = read(compiler_root / "manifest.json")
        require(set(compiler["changed_jar_entries"]) == PATCH_CLASSES, "Unexpected patched compiler classes")
        require(compiler["original_regression_exit_code"] != 0 and compiler["original_induction_regression_exit_code"] != 0 and
                compiler["original_interchange_regression_exit_code"] == 124, "Original compiler regressions were not reproduced")
        require(compiler["patched_regression_exit_code"] == 0 and set(compiler["patched_regressions"]) == HARNESSES,
                "Patched compiler regressions did not pass")
        for harness in HARNESSES:
            require("PASS:" in (compiler_root / "logs" / ("patched_" + harness + ".log")).read_text(), "Missing passing compiler regression")
        compiler_hash = digest(compiler_root / "lib/cetus.jar")
        require(compiler_hash == compiler["patched_jar_sha256"] == snapshot["cetus_jar_sha256"] == discovery["context"]["compiler_sha256"],
                "Compiler provenance disagrees")
        compilers.add(compiler_hash)
        total_runs += window_runs
        total_variants += total
        rows.append({"window": number, "loops": loops, "eligible_options": eligibility, "variants": total,
                     "distinct_executables": len(hashes), "cached_variants": result["cached_variant_count"],
                     "actual_executions": window_runs, "selected": result["selected"]["id"],
                     "configuration": result["selected"]["configuration"],
                     "serial_validation_seconds": result["serial_validation"]["window_seconds"],
                     "winner_validation_seconds": result["winner_validation"]["window_seconds"],
                     "validation_ratio": result["validation_window_speedup"],
                     "reordering_variants": sum(any(v["loop_interchange"] not in (0, "0") for v in r["configuration"].values()) for r in history[1:])})
    require(len(inputs) == len(compilers) == 1 and all(value == originals[0] for value in originals),
            "Independent windows used different input or compiler baselines")
    require(report["variant_count"] == total_variants and report["actual_execution_count"] == report["verified_execution_count"] == total_runs,
            "Aggregate counts disagree with audited executions")
    summary = {"status": "passed", "windows": rows, "variant_count": total_variants,
               "verified_application_executions": total_runs, "threads": expected_threads,
               "class": expected_class, "samples": manifest["samples"],
               "partition": expected_partition, "sequential_windows": True,
               "serial_exact_rhs_sha256": next(iter(inputs)), "compiler_sha256": next(iter(compilers)),
               "note": "Independent functional experiments; cached timings are not counted as new executions."}
    (root / "verified_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--partition", default="xlarge-mem")
    parser.add_argument("--class", dest="class_name", choices=("S", "W", "A", "B", "C"), default="S")
    args = parser.parse_args()
    print(json.dumps(verify(args.run_root, args.threads, args.partition, args.class_name), indent=2))
