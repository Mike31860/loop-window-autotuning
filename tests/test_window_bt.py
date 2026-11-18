"""Regression checks for direct interval measurement and input isolation."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from window_bt import (BTWindowEvaluator, CandidateError, TIMER, cetus_flags, instrument_serial,
                       outer_loop_spans, parse_window_time, validate_window,
                       perfect_nest_order, parse_legal_orders, loop_order_structure,
                       repair_legacy_timer_bound)


class BTWindowTests(unittest.TestCase):
    @staticmethod
    def legacy_timer_fixture():
        names = ["t_timer_%d" % index for index in range(1, 22)]
        names.append("TimeInstrument_Tuning_section_z_solve_S0")
        header = "\n".join("#define %s %d" % (name, index)
                           for index, name in enumerate(names, 1))
        header += "\n#define t_last 23\n#define " + TIMER + " 63\n"
        driver = "char *t_names[t_last+1];\n" + "\n".join(
            't_names[%s] = "%s";' % (name, name) for name in names)
        driver += '\nfor (i = 1; i <= t_last; i++) { printf("%s", t_names[i]); }\n'
        return header, driver

    def test_legacy_timer_bound_excludes_unnamed_23_and_preserves_timer_63(self):
        header, driver = self.legacy_timer_fixture()
        # These comments must not supply a name for the unused reporting slot.
        header += "/* #define t_last 99 */\n"
        driver += '/* t_names[t_last] = "unused"; */\n// t_names[t_last] = "unused";\n'
        repaired = repair_legacy_timer_bound(header, driver)
        self.assertEqual(repaired, header.replace("#define t_last 23", "#define t_last 22"))
        self.assertIn("#define TimeInstrument_Tuning_section_z_solve_S0 22\n", repaired)
        self.assertIn("#define " + TIMER + " 63\n", repaired)
        self.assertEqual(repair_legacy_timer_bound(repaired, driver), repaired)

    def test_legacy_timer_bound_rejects_gaps_unknown_constants_and_unexpected_bounds(self):
        header, driver = self.legacy_timer_fixture()
        assignment = 't_names[t_timer_11] = "t_timer_11";'
        for invalid_header, invalid_driver in (
                (header, driver.replace(assignment, "/* " + assignment + " */")),
                (header, driver.replace("t_names[t_timer_11]", "t_names[unknown_timer]")),
                (header.replace("#define t_last 23", "#define t_last 24"), driver),
                (header + "#define t_unnamed 23\n", driver),
                (header, driver.replace('"t_timer_11";', "NULL;"))):
            with self.subTest(header=invalid_header, driver=invalid_driver), self.assertRaises(CandidateError):
                repair_legacy_timer_bound(invalid_header, invalid_driver)

    def test_application_timeout_requires_positive_integer_seconds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for timeout in (0, -1, True, 1.5, "600", None):
                with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "Application timeout"):
                    BTWindowEvaluator(root, root / "backend", application_timeout=timeout)
            self.assertFalse((root / "backend").exists())

    def test_configured_timeout_applies_to_every_application_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cetus = root / "CETUS/cetus-tuning/bin/cetus"
            cetus.parent.mkdir(parents=True)
            cetus.touch()
            with patch.object(BTWindowEvaluator, "_prepare"):
                evaluator = BTWindowEvaluator(root, root / "backend", class_name="B", samples=4,
                                              application_timeout=900)
            evaluator.serial_text = "void exact_rhs(void){" + "for(i=0;i<2;i++){use(i);}" * 5 + "}"
            evaluator.object_paths = []
            loop_ids = ["exact_rhs#0", "exact_rhs#1"]
            configuration = {loop: {"induction": 0, "parallelize-loops": 0,
                                    "reduction": 0, "loop_interchange": 0} for loop in loop_ids}
            timings = iter((4, 1, 3, 2))

            def fake_run(command, cwd, log_prefix, **kwargs):
                if Path(log_prefix).name.startswith("application_"):
                    return "Verification = SUCCESSFUL\n" + TIMER + " = %d\n" % next(timings)
                Path(command[command.index("-o") + 1]).write_bytes(b"compiled fixture")
                return ""

            with patch.object(evaluator, "_run", side_effect=fake_run) as run, \
                    patch.object(evaluator, "_scope_proof", return_value={"passed": True}):
                result = evaluator.evaluate(loop_ids, configuration, root / "evaluation", serial=True)

            application_calls = [call for call in run.call_args_list
                                 if Path(call.args[2]).name.startswith("application_")]
            self.assertEqual(len(application_calls), 4)
            self.assertTrue(all(call.kwargs["timeout"] == 900 for call in application_calls))
            self.assertEqual(result["application_timeout_seconds"], 900)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["verification_count"], 4)
            self.assertEqual(result["samples"], [4, 1, 3, 2])
            self.assertEqual(result["window_seconds"], 2.5)

    def test_timer_surrounds_whole_window_including_intervening_statements(self):
        source = '''void exact_rhs(void) {
          int i, j;
          /* for(fake) { not a real loop } */
          const char *message = "for(fake) { }";
          for (i=0; i<10; ++i) { for(j=0;j<2;++j) { a[i]+=j; } }
          for (i=0; i<10; ++i) { b[i]=a[i]; }
          between_the_loops();
          for (i=0; i<10; ++i) { c[i]=b[i]; }
          for (i=0; i<10; ++i) { d[i]=c[i]; }
          for (i=0; i<10; ++i) { e[i]=d[i]; }
        }'''
        spans = outer_loop_spans(source)
        instrumented = instrument_serial(source, [1, 2])
        self.assertEqual(len(spans), 5)
        self.assertEqual(instrumented.count("timer_start("), 1)
        self.assertEqual(instrumented.count("timer_stop("), 1)
        actual = outer_loop_spans(instrumented)
        start = instrumented.index("timer_start(")
        stop = instrumented.index("timer_stop(")
        self.assertLess(actual[0][1], start)
        self.assertLess(start, actual[1][0])
        self.assertLess(start, instrumented.index("between_the_loops();"))
        self.assertLess(instrumented.index("between_the_loops();"), stop)
        self.assertLess(actual[2][1], stop)
        self.assertLess(stop, actual[3][0])
        for index in (0, 3, 4):
            self.assertEqual(source[slice(*spans[index])],
                             instrumented[slice(*actual[index])])

    def test_window_timing_requires_one_finite_positive_measurement(self):
        self.assertEqual(parse_window_time(TIMER + " = 0.012345678\n"), 0.012345678)
        for text in ("", TIMER + " = NaN", TIMER + " = inf", TIMER + " = 0",
                     TIMER + " = -1", (TIMER + " = 1\n") * 2):
            with self.assertRaises(CandidateError):
                parse_window_time(text)

    def test_payload_contains_distinct_loop_settings_and_disables_global_passes(self):
        ids = ["exact_rhs#1", "exact_rhs#2"]
        config = {ids[0]: {"induction": 3, "parallelize-loops": 0, "reduction": 0},
                  ids[1]: {"induction": 0, "parallelize-loops": 1, "reduction": 2}}
        flags = cetus_flags(ids, config)
        self.assertIn("-tinline=0", flags)
        self.assertIn("-teliminate-branch=0", flags)
        payload = [flag for flag in flags if flag.startswith("-Tuning_Analysis=")][0]
        members = payload.split("<", 1)[1].split("&")
        decoded = {}
        for member in members:
            loop_id, fields = member.split("%")
            decoded[loop_id] = dict(field.split("=", 1) for field in fields.split(","))
        self.assertEqual(set(decoded), set(ids))
        for loop_id in ids:
            for option, value in config[loop_id].items():
                self.assertEqual(decoded[loop_id][option], str(value))
            self.assertEqual(decoded[loop_id]["loop_interchange"], "0")
        with self.assertRaises(ValueError):
            validate_window(["exact_rhs#0", "exact_rhs#2"], config)

    def test_requested_order_survives_the_cetus_protocol(self):
        ids = ["exact_rhs#0", "exact_rhs#1"]
        config = {loop: {"induction": 0, "parallelize-loops": 0,
                         "reduction": 0, "loop_interchange": 0} for loop in ids}
        config[ids[0]]["loop_interchange"] = "m_i_j_k"
        payload = cetus_flags(ids, config)[-1]
        self.assertIn("loop_interchange=m_i_j_k&exact_rhs#1%", payload)
        self.assertTrue(payload.endswith("loop_interchange=0"))
        config[ids[0]]["loop_interchange"] = "k_k_i_m"
        with self.assertRaises(ValueError):
            cetus_flags(ids, config)

    def test_order_discovery_uses_only_the_target_loop_and_validates_permutations(self):
        text = ("LoopName: exact_rhs#4 Legal Loop Interchange Transformations: [a b, b a]\n"
                "LoopName: exact_rhs#0 Legal Loop Interchange Transformations: [k j i m, m i j k]\n")
        orders, reasons = parse_legal_orders(text, "exact_rhs#0", ["k", "j", "i", "m"])
        self.assertEqual(orders, ["k_j_i_m", "m_i_j_k"])
        self.assertEqual(reasons, [])
        with self.assertRaises(CandidateError):
            parse_legal_orders(text, "exact_rhs#1", ["k", "j", "i", "m"])
        with self.assertRaises(CandidateError):
            parse_legal_orders(text.replace("m i j k", "m i i k"), "exact_rhs#0", ["k", "j", "i", "m"])
        with self.assertRaises(CandidateError):
            parse_legal_orders(text + text, "exact_rhs#0", ["k", "j", "i", "m"])

    def test_perfect_nest_detection_excludes_intervening_statements(self):
        perfect = "for(k=0;k<2;k++){for(j=0;j<3;j++){for(i=0;i<4;i++){a[k][j][i]=0;}}}"
        self.assertEqual(perfect_nest_order(perfect), ["k", "j", "i"])
        self.assertIsNone(perfect_nest_order(perfect.replace("for(j", "prepare();for(j")))

    def test_loop_structure_preserves_branches_and_repeated_sibling_indices(self):
        source = '''for (k=0;k<2;k++) {
          prepare();
          for (j=0;j<3;j++) {
            for (i=0;i<4;i++) {
              for (m=0;m<5;m++) { first(); }
              between();
              for (m=1;m<5;m++) { second(); }
            }
            for (i=1;i<3;i++) { other(); }
          }
        }'''
        self.assertIsNone(perfect_nest_order(source))
        self.assertEqual(loop_order_structure(source), [{"index": "k", "children": [
            {"index": "j", "children": [
                {"index": "i", "children": [
                    {"index": "m", "children": []}, {"index": "m", "children": []}]},
                {"index": "i", "children": []}]}]}])

    def test_loop_structure_ignores_noncode_and_keeps_perfect_order(self):
        source = '''/* for(fake=0;fake<2;fake++) { } */
          #pragma cetus private(i,j)
          for (int i=0;i<2;i++) {
            const char *text = "for(fake=0;fake<2;fake++) { }";
            #pragma omp parallel for
            for (j=start(1,2);j<3;j++) { use(i,j); }
          }
          for (k=0;k<4;k++) { finish(); }
        '''
        self.assertEqual(loop_order_structure(source), [
            {"index": "i", "children": [{"index": "j", "children": []}]},
            {"index": "k", "children": []}])
        perfect = "for(k=0;k<2;k++){for(j=0;j<3;j++){for(i=0;i<4;i++){use();}}}"
        self.assertEqual(loop_order_structure(perfect), [{"index": "k", "children": [
            {"index": "j", "children": [{"index": "i", "children": []}]}]}])

    def test_loop_structure_rejects_unsupported_forms_instead_of_guessing(self):
        for source in ("for (;;) { use(); }", "for (i=0;i<2;i++) use();",
                       "for (i=0,j=0;i<2;i++) { use(); }",
                       "for (i=0;i<2;i++) { while (ready()) { use(); } }",
                       "for (i=0;i<2;i++) { do { use(); } while (ready()); }",
                       "int i=0;"):
            with self.subTest(source=source), self.assertRaises(CandidateError):
                loop_order_structure(source)

    def test_transformation_evidence_records_original_and_emitted_structures(self):
        first = "for(i=0;i<2;i++){for(j=0;j<3;j++){a[i][j]=1;}}"
        rest = "for(k=0;k<2;k++){use();}" * 4
        reference = "void exact_rhs(void){" + first + rest + "}"
        reordered = "for(j=0;j<3;j++){for(i=0;i<2;i++){a[i][j]=1;}}"
        actual = "void exact_rhs(void){" + reordered + rest + "}"
        evaluator = object.__new__(BTWindowEvaluator)
        evaluator.discoveries = {"exact_rhs#0": {"options": {"loop_interchange": [0, "j_i"]}}}
        configuration = {"exact_rhs#0": {"induction": 0, "parallelize-loops": 0,
                                          "reduction": 0, "loop_interchange": "j_i"}}
        evidence = evaluator._transformation_evidence(actual, reference, ["exact_rhs#0"],
                                                       configuration)["exact_rhs#0"]
        self.assertEqual(evidence["original_loop_structure"], [
            {"index": "i", "children": [{"index": "j", "children": []}]}])
        self.assertEqual(evidence["emitted_loop_structure"], [
            {"index": "j", "children": [{"index": "i", "children": []}]}])
        self.assertEqual(evidence["original_order"], ["i", "j"])
        self.assertEqual(evidence["emitted_order"], ["j", "i"])
        self.assertTrue(evidence["interchange_applied"])


if __name__ == "__main__":
    unittest.main()
