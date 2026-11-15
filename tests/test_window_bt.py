"""Regression checks for direct interval measurement and input isolation."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from window_bt import (CandidateError, TIMER, cetus_flags, instrument_serial,
                       outer_loop_spans, parse_window_time, validate_window,
                       perfect_nest_order, parse_legal_orders)


class BTWindowTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
