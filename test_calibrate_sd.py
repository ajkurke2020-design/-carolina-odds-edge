import unittest
from statistics import NormalDist

import calibrate_sd


def line_row(event_id, line, probability, *, market="spreads", selection="Home",
             quote_time="2026-01-01T12:00:00+00:00", sport="NBA"):
    return {
        "sport": sport,
        "event_id": event_id,
        "market": market,
        "period": "",
        "team": "",
        "selection": selection,
        "line": line,
        "fair_probability": probability,
        "quote_time": quote_time,
    }


def probability_for_sd(line, sd):
    # Normal model in which the selection's probability at `line` is Phi(-line / sd).
    return NormalDist(0, sd).cdf(-line)


class ImpliedSdTests(unittest.TestCase):
    def test_recovers_true_standard_deviation_from_two_lines(self):
        sd = calibrate_sd.implied_sd(
            -2.5, probability_for_sd(-2.5, 15), -4.5, probability_for_sd(-4.5, 15)
        )
        self.assertAlmostEqual(sd, 15.0, places=6)

    def test_invalid_probabilities_or_identical_lines_give_none(self):
        self.assertIsNone(calibrate_sd.implied_sd(-2.5, 0.0, -4.5, 0.4))
        self.assertIsNone(calibrate_sd.implied_sd(-2.5, 0.5, -2.5, 0.4))
        self.assertIsNone(calibrate_sd.implied_sd(-2.5, 0.5, -4.5, 0.5))


class EstimateSdsTests(unittest.TestCase):
    def test_mirrored_selections_are_not_double_counted(self):
        rows = []
        for line in (-2.5, -4.5):
            rows.append(line_row("g1", line, probability_for_sd(line, 15), selection="Home"))
            rows.append(
                line_row("g1", -line, 1 - probability_for_sd(line, 15), selection="Away")
            )
        estimates = calibrate_sd.estimate_sds(rows)
        self.assertEqual(len(estimates), 1)

    def test_single_line_events_contribute_nothing(self):
        rows = [line_row("g1", -3.5, 0.55)]
        self.assertEqual(calibrate_sd.estimate_sds(rows), [])

    def test_latest_snapshot_is_used_for_a_repeated_line(self):
        rows = [
            line_row("g1", -2.5, 0.10, quote_time="2026-01-01T10:00:00+00:00"),
            line_row("g1", -2.5, probability_for_sd(-2.5, 15), quote_time="2026-01-01T11:00:00+00:00"),
            line_row("g1", -4.5, probability_for_sd(-4.5, 15)),
        ]
        estimates = calibrate_sd.estimate_sds(rows)
        self.assertAlmostEqual(estimates[0][2], 15.0, places=6)

    def test_unsupported_markets_are_ignored(self):
        rows = [
            line_row("g1", -2.5, 0.5, market="moneyline"),
            line_row("g1", -4.5, 0.4, market="moneyline"),
        ]
        self.assertEqual(calibrate_sd.estimate_sds(rows), [])


class SummarizeTests(unittest.TestCase):
    def test_median_and_sample_counts_are_reported_per_sport_and_market(self):
        summary = calibrate_sd.summarize(
            [
                ("NBA", "spreads", 10.0),
                ("NBA", "spreads", 14.0),
                ("NBA", "spreads", 100.0),
                ("NBA", "totals", 16.0),
            ]
        )
        self.assertEqual(summary[("NBA", "spreads")], {"median_sd": 14.0, "samples": 3})
        self.assertEqual(summary[("NBA", "totals")], {"median_sd": 16.0, "samples": 1})


if __name__ == "__main__":
    unittest.main()
