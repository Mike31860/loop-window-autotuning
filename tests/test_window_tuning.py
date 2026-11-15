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
