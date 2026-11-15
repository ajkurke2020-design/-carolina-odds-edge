import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import database


class OpportunityLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "bets.db")
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        database.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_scan_log_finalizes_clv_from_latest_matching_prestart_pinnacle_quote(self):
        opportunity = {
            "sport": "NBA",
            "event_id": "game-1",
            "matchup": "Home vs Away",
            "kickoff": "2026-01-01T13:00:00Z",
            "market": "Moneyline",
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "sportsbook": "DraftKings",
            "bookmaker_key": "draftkings",
            "line": None,
            "odds_american": 140,
            "odds_decimal": 2.4,
            "fair_probability": 0.5,
            "ev_pct": 20.0,
            "kelly_stake": 10.0,
            "pinnacle_american_this_side": -110,
            "pinnacle_american_other_side": -110,
            "pinnacle_overround": 1.0476,
            "pinnacle_quote_time": "2026-01-01T11:59:30Z",
            "book_quote_time": "2026-01-01T11:59:30Z",
            "flags": "verify",
        }
        snapshot = {
            "sport": "NBA",
            "event_id": "game-1",
            "market_key": "h2h",
            "period": "",
            "team": "",
            "selection": "Home",
            "point": None,
            "kickoff": "2026-01-01T13:00:00Z",
            "fair_probability": 0.55,
            "pinnacle_american": -120,
            "pinnacle_decimal": 1.833333,
            "overround": 1.04,
            "quote_time": "2026-01-01T12:59:30Z",
        }

        database.log_scan(
            "scan-1",
            "2026-01-01T12:59:35Z",
            [opportunity],
            [snapshot],
        )
        finalized = database.finalize_started_opportunities(
            datetime(2026, 1, 1, 13, 0, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(finalized, 1)

        log = database.load_opportunity_log()
        self.assertEqual(len(log), 1)
        self.assertAlmostEqual(log.loc[0, "closing_fair_probability"], 0.55)
        self.assertAlmostEqual(log.loc[0, "clv_pct"], 32.0)
        self.assertEqual(log.loc[0, "closing_quote_time"], "2026-01-01T12:59:30+00:00")

    def test_init_db_configures_wal_and_busy_timeout(self):
        pragmas = []
        original_connect = sqlite3.connect

        class RecordingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                result = super().execute(sql, parameters)
                if sql in ("PRAGMA journal_mode=WAL;", "PRAGMA busy_timeout=5000;"):
                    pragmas.append((sql, result.fetchone()[0]))
                return result

        def recording_connect(*args, **kwargs):
            kwargs["factory"] = RecordingConnection
            return original_connect(*args, **kwargs)

        with patch.object(database.sqlite3, "connect", side_effect=recording_connect):
            database.init_db()

        self.assertEqual(
            pragmas,
            [
                ("PRAGMA journal_mode=WAL;", "wal"),
                ("PRAGMA busy_timeout=5000;", 5000),
            ],
        )

    def test_line_adjusted_clv_uses_closing_pinnacle_lines_at_venue_line(self):
        opportunity = {
            "sport": "NBA",
            "event_id": "game-2",
            "matchup": "Home vs Away",
            "kickoff": "2026-01-01T13:00:00Z",
            "market": "Home -3.5",
            "market_key": "spreads",
            "period": "",
            "team": "",
            "selection": "Home",
            "sportsbook": "DraftKings",
            "bookmaker_key": "draftkings",
            "line": -3.5,
            "pinnacle_line": -2.5,
            "pinnacle_lines_used": "-4.5, -2.5",
            "odds_american": 140,
            "odds_decimal": 2.4,
            "fair_probability": 0.5,
            "ev_pct": 20.0,
            "kelly_stake": 10.0,
            "pinnacle_american_this_side": -110,
            "pinnacle_american_other_side": -110,
            "pinnacle_overround": 1.04,
            "pinnacle_quote_time": "2026-01-01T11:59:30Z",
            "book_quote_time": "2026-01-01T11:59:30Z",
            "flags": "line-adjusted",
            "pricing_method": "line-adjusted",
            "line_adjustment_method": "interpolation",
            "win_probability": 0.5,
            "lose_probability": 0.5,
            "push_probability": 0.0,
        }
        snapshots = [
            {
                "sport": "NBA",
                "event_id": "game-2",
                "market_key": "spreads",
                "period": "",
                "team": "",
                "selection": "Home",
                "point": point,
                "kickoff": "2026-01-01T13:00:00Z",
                "fair_probability": probability,
                "pinnacle_american": -110,
                "pinnacle_decimal": 1.9,
                "overround": 1.04,
                "quote_time": "2026-01-01T12:59:30Z",
            }
            for point, probability in ((-2.5, 0.6), (-4.5, 0.4))
        ]

        database.log_scan(
            "scan-adjusted",
            "2026-01-01T12:59:35Z",
            [opportunity],
            snapshots,
        )
        finalized = database.finalize_started_opportunities(
            datetime(2026, 1, 1, 13, 0, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(finalized, 1)

        log = database.load_opportunity_log()
        self.assertEqual(log.loc[0, "pricing_method"], "line-adjusted")
        self.assertEqual(log.loc[0, "line_adjustment_method"], "interpolation")
        self.assertAlmostEqual(log.loc[0, "closing_fair_probability"], 0.5)
        self.assertAlmostEqual(log.loc[0, "clv_pct"], 20.0)


if __name__ == "__main__":
    unittest.main()
