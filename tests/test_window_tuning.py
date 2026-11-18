"""Search-contract tests; actual compilation and numerical checks run on DARWIN."""
import copy
import contextlib
import io
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import window_tuning as tuning


LOOPS = ["exact_rhs#%d" % i for i in range(5)]


def loop_structure(order):
    children = []
    for index in reversed(order.split("_")):
        children = [{"index": index, "children": children}]
    return children


def report_result(loop_id, measurement):
    return {"window_number": 1, "loop_ids": [loop_id],
            "selected": {"configuration": tuning.serial_configuration([loop_id])},
            "winner_validation": {"window_seconds": 1.0, **measurement},
            "serial_validation": {"window_seconds": 2.0}}


class CandidateFailure(Exception):
    pass


class FakeEvaluator:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure
        self.failed = False
        self.discovery_calls = []
        self.cache = {}

    def discover(self, loop_ids, artifact_dir):
        self.discovery_calls.append(tuple(loop_ids))
        return eligibility(loop_ids)

    def evaluate(self, loop_ids, configuration, artifact_dir, serial=False, use_cache=True):
        self.calls.append((tuple(loop_ids), copy.deepcopy(configuration), serial, use_cache))
        if not serial and self.failure and not self.failed:
            self.failed = True
            raise self.failure("deliberate candidate failure")
        # Neither change alone helps: this requires the jointly chosen pair.
        first = configuration[loop_ids[0]]
        last = configuration[loop_ids[-1]]
        together = first["induction"] == 3 and last["parallelize-loops"] == 1
        elapsed = 4.0 if together and not serial else 10.0
        binary = hashlib.sha256(json.dumps((loop_ids, first["induction"],
                                            last["parallelize-loops"])).encode()).hexdigest()
        cache_hit = use_cache and binary in self.cache
        record = {
            "window_seconds": elapsed,
            "samples": [elapsed, elapsed, elapsed],
            "verification_count": 0 if cache_hit else 3,
            "inherited_verification_count": 3 if cache_hit else 0,
            "actual_execution_count": 0 if cache_hit else 3,
            "cache_hit": cache_hit,
            "measured": not cache_hit,
            "executable_sha256": binary,
            "input_sha256": "same-serial-input",
            "measurement_source": self.cache.get(binary, str(Path(artifact_dir) / "evaluation.json")),
            "executable": str(Path(artifact_dir) / "output"),
            "generated_c": str(Path(artifact_dir) / "exact_rhs.c"),
            "scope_proof": {"outside_window_unchanged": True},
            "loop_structures": {
                loop: loop_structure(configuration[loop]["loop_interchange"]
                                     if configuration[loop]["loop_interchange"] else "k_j_i_m")
                for loop in loop_ids},
        }
        if use_cache and not cache_hit:
            self.cache[binary] = str(Path(artifact_dir) / "evaluation.json")
        return record


def eligibility(loop_ids):
    return {"schema_version": 1, "loops": {
        loop: {"options": {"induction": [0, 3], "parallelize-loops": [0, 1],
                           "reduction": [0, 2], "loop_interchange": [0]},
               "original_order": ["k", "j", "i", "m"], "evidence": {}}
        for loop in loop_ids}, "context": {"inlining": 0}}


class WindowTests(unittest.TestCase):
    def setUp(self):
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_adjacent_windows_and_boundaries(self):
        self.assertEqual(tuning.generate_windows(LOOPS, 2),
                         [LOOPS[i:i + 2] for i in range(4)])
        self.assertEqual(tuning.generate_windows(LOOPS, 1), [[x] for x in LOOPS])
        self.assertEqual(tuning.generate_windows(LOOPS, 5), [LOOPS])
        for width in (0, -1, 6):
            with self.assertRaises(ValueError):
                tuning.generate_windows(LOOPS, width)

    def test_joint_configuration_space_and_no_shared_mutable_settings(self):
        allowed = tuning.validate_eligibility(LOOPS[:2], eligibility(LOOPS[:2]))
        choices = list(tuning.candidate_configurations(LOOPS[:2], allowed))
        self.assertEqual(len(choices), 64)
        signatures = {tuple(tuple(sorted(c[loop].items())) for loop in LOOPS[:2])
                      for c in choices}
        self.assertEqual(len(signatures), 64)
        self.assertTrue(any(c[LOOPS[0]] != c[LOOPS[1]] for c in choices))
        untouched = copy.deepcopy(choices[1:])
        choices[0][LOOPS[0]]["induction"] = 999
        self.assertEqual(choices[1:], untouched)

    def test_joint_window_objective_overrides_individual_loop_ranking(self):
        records = [
            {"status": "ok", "measurement": {"window_seconds": 9.0,
                                                "loop_seconds": [7.0, 2.0]}},
            {"status": "ok", "measurement": {"window_seconds": 12.0,
                                                "loop_seconds": [2.0, 10.0]}},
            {"status": "failed", "measurement": {"window_seconds": 0.01}},
        ]
        self.assertIs(tuning.select_best(records), records[0])

    def test_each_window_starts_serial_and_only_contains_its_own_loops(self):
        evaluator = FakeEvaluator()
        with tempfile.TemporaryDirectory() as tmp:
            for number, loops in enumerate((LOOPS[:2], LOOPS[1:3]), 1):
                start = len(evaluator.calls)
                tuning.search_window(loops, evaluator, Path(tmp) / str(number),
                                     window_number=number)
                calls = evaluator.calls[start:]
                self.assertTrue(calls[0][2])
                self.assertEqual(evaluator.discovery_calls[-1], tuple(loops))
                for ids, configuration, serial, use_cache in calls:
                    self.assertEqual(set(configuration), set(loops))
                    self.assertEqual(ids, tuple(loops))
                    if serial:
                        self.assertTrue(all(value == 0 for options in configuration.values()
                                            for value in options.values()))
                # A combined change missed by a one-coordinate search is replayed.
                final_candidate = [call for call in calls if not call[2]][-1][1]
                self.assertEqual(final_candidate[loops[0]]["induction"], 3)
                self.assertEqual(final_candidate[loops[1]]["parallelize-loops"], 1)
                self.assertFalse(calls[-1][3])
                self.assertFalse(calls[-2][3])
                text = (Path(tmp) / str(number) / "results.txt").read_text()
                self.assertIn("Window#%d:" % number, text)
                for loop in loops:
                    self.assertIn("  " + loop + " : Opt : [", text)
                self.assertIn("WindowExecutionTime = 4.000000000 s", text)
                self.assertIn("SerialExecutionTime = 10.000000000 s", text)
                self.assertIn("SpeedupVsSerial = 2.500000x", text)

    def test_text_report_preserves_per_loop_settings_and_uses_fresh_window_times(self):
        loops = ["kernel_3mm#3", "kernel_3mm#4"]
        result = {
            "window_number": 4, "loop_ids": loops,
            "selected": {"id": "variant_000003", "kind": "cetus", "configuration": {
                loops[0]: {"parallelize-loops": 1, "induction": 0,
                           "reduction": 2, "loop_interchange": "k_i_j"},
                loops[1]: {"parallelize-loops": 0, "induction": 3,
                           "reduction": 0, "loop_interchange": 0}},
                "measurement": {"window_seconds": 0.1}},
            "serial_baseline": {"window_seconds": 10.0},
            "winner_validation": {"window_seconds": 6.0, "samples": [5.0, 6.0, 7.0],
                                  "repetitions": 10, "loop_structures": {
                                      loops[0]: loop_structure("k_i_j"),
                                      loops[1]: loop_structure("i_j_k")},
                                  "transformations": {
                                      loops[0]: {"original_order": ["i", "j", "k"],
                                                 "emitted_order": ["k", "i", "j"],
                                                 "interchange_applied": True},
                                      loops[1]: {"original_order": ["i", "j", "k"],
                                                 "emitted_order": ["i", "j", "k"],
                                                 "interchange_applied": False}}},
            "serial_validation": {"window_seconds": 3.0, "samples": [2.0, 3.0, 4.0],
                                  "repetitions": 10},
            "search_window_speedup": 100.0, "validation_window_speedup": 0.5,
            "elapsed_seconds": 999.0,
        }
        before = copy.deepcopy(result)
        text = tuning.format_window_result(result)
        self.assertIn("Window#4:", text)
        self.assertIn("  kernel_3mm#3 : Opt : [Parallelization=On, Induction=Off, "
                      "Reduction=On, LoopInterchange=On, LoopOrder=k_i_j]", text)
        self.assertIn("  kernel_3mm#4 : Opt : [Parallelization=Off, Induction=On, "
                      "Reduction=Off, LoopInterchange=Off, LoopOrder=i_j_k]", text)
        # A promising search result can be slower during fresh validation.
        self.assertIn("WindowExecutionTime = 6.000000000 s", text)
        self.assertIn("SerialExecutionTime = 3.000000000 s", text)
        self.assertIn("SpeedupVsSerial = 0.500000x", text)
        summary = tuning.format_results_text([result])
        self.assertRegex(summary.lower(), r"fresh[- ]validation.*median|median.*fresh[- ]validation")
        self.assertIn("Samples = 3", text)
        self.assertIn("Repetitions = 10", text)
        self.assertEqual(result, before)

    def test_text_report_preserves_ordered_and_repeated_sibling_loops(self):
        structure = [{"index": "k", "children": [{"index": "j", "children": [
            *loop_structure("i_m"), *loop_structure("i_m"), *loop_structure("m")]}]}]
        result = report_result(LOOPS[1], {"loop_structures": {LOOPS[1]: structure}})
        before = copy.deepcopy(result)
        text = tuning.format_window_result(result)
        self.assertIn("LoopInterchange=Off, LoopOrder=k_j_(i_m; i_m; m)]", text)
        self.assertNotIn("LoopOrder=Off", text)
        self.assertEqual(result, before)

    def test_text_report_reads_saved_transformation_order_evidence(self):
        evidence = {LOOPS[0]: {"original_order": ["k", "j", "i", "m"],
                               "emitted_order": ["k", "j", "i", "m"]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transformations.json"
            path.write_text(json.dumps(evidence))
            for saved in (evidence, str(path)):
                with self.subTest(evidence_type=type(saved).__name__):
                    result = report_result(LOOPS[0], {"transformations": saved})
                    self.assertIn("LoopInterchange=Off, LoopOrder=k_j_i_m]", tuning.format_window_result(result))
            # Inline metadata remains sufficient when old artifact paths are unavailable.
            result["winner_validation"]["transformations"] = str(Path(tmp) / "missing.json")
            result["winner_validation"]["loop_structures"] = {LOOPS[0]: loop_structure("k_j_i_m")}
            self.assertIn("LoopInterchange=Off, LoopOrder=k_j_i_m]", tuning.format_window_result(result))

    def test_text_report_recovers_imperfect_order_from_verified_saved_source(self):
        source = '''void exact_rhs(void) {
          for(k=0;k<2;k++){for(j=0;j<3;j++){a[k][j]=0;}}
          for(k=0;k<2;k++){for(j=0;j<3;j++){
            prepare();
            for(i=0;i<4;i++){for(m=0;m<5;m++){a[k][j]+=m;}}
            for(i=0;i<4;i++){a[k][j]+=i;}
          }}
          for(i=0;i<4;i++){b[i]=0;}
          for(i=0;i<4;i++){c[i]=0;}
          for(i=0;i<4;i++){d[i]=0;}
        }'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated_exact_rhs.c"
            path.write_text(source)
            result = report_result(LOOPS[1], {
                "transformations": {LOOPS[1]: {"original_order": None, "emitted_order": None}},
                "generated_c": str(path), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            self.assertIn("LoopInterchange=Off, LoopOrder=k_j_(i_m; i)]", tuning.format_window_result(result))
            path.write_text(source.replace("prepare();", "changed();"))
            with self.assertRaises(ValueError):
                tuning.format_window_result(result)

    def test_text_report_does_not_invent_missing_original_order(self):
        result = report_result(LOOPS[0], {})
        with self.assertRaisesRegex(ValueError, r"(?i)order|structure"):
            tuning.format_window_result(result)

    def test_run_text_summary_saves_only_completed_windows_and_current_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = tuning.search_window(LOOPS[3:5], FakeEvaluator(), directory / "window_04",
                                          window_number=4)
            for status in ("running", "failed", "complete"):
                with self.subTest(status=status):
                    tuning._run_summary(directory, {"samples": 3}, [result], status,
                                        error={"message": "next window failed"} if status == "failed" else None)
                    text = (directory / "results.txt").read_text()
                    self.assertRegex(text, r"(?im)^(?:Run)?Status\s*[:=]\s*" + status + r"$")
                    self.assertEqual(text.count("Window#"), 1)
                    self.assertIn("Window#4:", text)
                    self.assertIn("exact_rhs#3 : Opt : [", text)
                    self.assertIn("exact_rhs#4 : Opt : [", text)
                    self.assertIn("WindowExecutionTime = 4.000000000 s", text)
                    self.assertIn("SpeedupVsSerial = 2.500000x", text)
                    self.assertEqual(json.loads((directory / "results.json").read_text())["status"], status)
            tuning._run_summary(directory, {}, [], "running")
            self.assertNotIn("Window#", (directory / "results.txt").read_text())

    def test_discovered_choices_control_variant_count_and_legal_orders(self):
        loops = LOOPS[:2]
        discovered = eligibility(loops)
        first, second = (discovered["loops"][loop]["options"] for loop in loops)
        first.update(induction=[0], reduction=[0], loop_interchange=[0, "j_k_i_m"])
        second.update(induction=[0], reduction=[0])
        allowed = tuning.validate_eligibility(loops, discovered)
        choices = list(tuning.candidate_configurations(loops, allowed))
        self.assertEqual(len(choices), 8)
        self.assertEqual({c[loops[0]]["loop_interchange"] for c in choices}, {0, "j_k_i_m"})
        self.assertTrue(all(c[loops[1]]["loop_interchange"] == 0 for c in choices))
        self.assertTrue(all(c[loop]["induction"] == c[loop]["reduction"] == 0
                            for c in choices for loop in loops))

    def test_cached_variants_keep_provenance_without_counting_new_executions(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = tuning.search_window(LOOPS[:2], FakeEvaluator(), Path(tmp) / "window")
            records = json.loads(Path(result["history_file"]).read_text())
            cached = [r for r in records if r.get("measurement", {}).get("cache_hit")]
            self.assertTrue(cached)
            for record in cached:
                measurement = record["measurement"]
                self.assertEqual(measurement["actual_execution_count"], 0)
                self.assertEqual(measurement["verification_count"], 0)
                self.assertTrue(measurement["measurement_source"])

    def test_invalid_candidate_is_excluded_but_infrastructure_error_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            evaluator = FakeEvaluator(CandidateFailure)
            tuning.search_window(LOOPS[:2], evaluator, Path(tmp) / "invalid",
                                 candidate_error_type=(CandidateFailure,))
            self.assertTrue(evaluator.failed)
            with self.assertRaises(RuntimeError):
                tuning.search_window(LOOPS[:2], FakeEvaluator(RuntimeError),
                                     Path(tmp) / "infrastructure",
                                     candidate_error_type=(CandidateFailure,))

    def test_configuration_selects_a_function_without_hardcoding_java(self):
        config = Path(__file__).resolve().parents[1] / "configs" / "procedures_BT_exact.txt"
        function, loops = tuning.parse_procedure_config(config)
        self.assertEqual(function, "exact_rhs")
        self.assertEqual(loops, LOOPS)

    def test_rejects_missing_end_duplicate_and_nonadjacent_loop_ids(self):
        malformed = [
            "FUNCTION_NAME:exact_rhs\nLOOP_ID:exact_rhs#0;DESIRED:none\n",
            "FUNCTION_NAME:exact_rhs\nLOOP_ID:exact_rhs#0;DESIRED:none\n"
            "LOOP_ID:exact_rhs#0;DESIRED:none\nEND\n",
            "FUNCTION_NAME:exact_rhs\nLOOP_ID:exact_rhs#0;DESIRED:none\n"
            "LOOP_ID:exact_rhs#2;DESIRED:none\nEND\n",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.txt"
            for content in malformed:
                path.write_text(content)
                with self.assertRaises(ValueError):
                    tuning.parse_procedure_config(path)

    def test_invalid_window_measurements_cannot_win(self):
        for elapsed in (float("nan"), float("inf"), -1, 0, True):
            with self.assertRaises(ValueError):
                tuning.select_best([{"status": "ok", "measurement": {
                    "window_seconds": elapsed}}])


if __name__ == "__main__":
    unittest.main()
