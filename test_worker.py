import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from worker import (
    ACTIVE_SPORTS,
    MAX_HISTORY_AGE_SECONDS,
    build_alert_key,
    clean_expired_history,
    format_telegram_alert,
    is_game_started,
    load_alert_history,
    load_cached_odds,
    run_worker,
    save_alert_history,
    save_cached_odds,
    send_telegram_message,
    should_alert,
)


class WorkerTests(unittest.TestCase):
    def test_active_sports_configuration(self):
        self.assertEqual(
            ACTIVE_SPORTS,
            ["Tennis", "NCAAB", "NBA", "NHL", "NFL", "NCAAF"],
        )
        self.assertNotIn("MLB", ACTIVE_SPORTS)

    def test_is_game_started(self):
        now = datetime(2026, 11, 17, 20, 0, 0, tzinfo=timezone.utc)
        past = (now - timedelta(minutes=5)).isoformat()
        future = (now + timedelta(hours=2)).isoformat()

        self.assertTrue(is_game_started(past, now))
        self.assertTrue(is_game_started(now.isoformat(), now))
        self.assertFalse(is_game_started(future, now))
        self.assertFalse(is_game_started(None, now))
        self.assertFalse(is_game_started("invalid-time", now))

    def test_build_alert_key_with_point_and_line(self):
        opp1 = {
            "event_id": "game-101",
            "market": "Spread -3.5",
            "selection": "North Carolina",
            "line": -3.5,
            "sportsbook": "DraftKings",
        }
        self.assertEqual(
            build_alert_key(opp1),
            "game-101_Spread -3.5_North Carolina_-3.5_DraftKings",
        )

        opp2 = {
            "game_id": "game-202",
            "market_key": "h2h",
            "selection": "Duke",
            "sportsbook": "FanDuel",
        }
        self.assertEqual(
            build_alert_key(opp2),
            "game-202_h2h_Duke_None_FanDuel",
        )

    def test_should_alert_new_key(self):
        history = {}
        self.assertTrue(should_alert("key1", 3.5, history))

    def test_should_alert_unchanged_or_lower_ev(self):
        history = {"key1": {"ev": 4.0, "timestamp": time.time()}}
        self.assertFalse(should_alert("key1", 4.0, history))
        self.assertFalse(should_alert("key1", 3.5, history))
        self.assertFalse(should_alert("key1", 5.4, history))  # +1.4% increase < +1.5%

    def test_should_alert_significant_ev_increase(self):
        history = {"key1": {"ev": 4.0, "timestamp": time.time()}}
        self.assertTrue(should_alert("key1", 5.5, history))  # exactly +1.5%
        self.assertTrue(should_alert("key1", 6.0, history))  # +2.0%

    def test_clean_expired_history(self):
        now = 1000000.0
        history = {
            "fresh": {"ev": 3.5, "timestamp": now - 3600},  # 1 hour ago
            "borderline": {"ev": 4.0, "timestamp": now - MAX_HISTORY_AGE_SECONDS},
            "expired": {"ev": 5.0, "timestamp": now - (MAX_HISTORY_AGE_SECONDS + 10)},
            "malformed": {"ev": 3.0},
        }
        cleaned = clean_expired_history(history, now)
        self.assertIn("fresh", cleaned)
        self.assertIn("borderline", cleaned)
        self.assertNotIn("expired", cleaned)
        self.assertNotIn("malformed", cleaned)

    def test_format_telegram_alert_contains_required_fields(self):
        opp = {
            "sport": "NBA",
            "matchup": "Boston Celtics vs Dallas Mavericks",
            "market": "Spread -4.5",
            "selection": "Boston Celtics",
            "line": -4.5,
            "odds_american": -110,
            "odds_decimal": 1.91,
            "sportsbook": "DraftKings",
            "fair_probability": 0.55,
            "ev_pct": 5.05,
        }
        text = format_telegram_alert(opp)
        self.assertIn("Sport:</b> NBA", text)
        self.assertIn("Matchup:</b> Boston Celtics vs Dallas Mavericks", text)
        self.assertIn("Bet / Line:</b> Boston Celtics -4.5", text)
        self.assertIn("Odds:</b> -110 (1.91)", text)
        self.assertIn("Sportsbook:</b> DraftKings", text)
        self.assertIn("Fair Odds:</b>", text)
        self.assertIn("EV%:</b> +5.05%", text)

    def test_format_telegram_alert_escapes_html(self):
        opp = {
            "sport": "NCAAF",
            "matchup": "Texas A&M vs LSU",
            "market": "Moneyline",
            "selection": "Texas A&M",
            "odds_american": 150,
            "odds_decimal": 2.5,
            "sportsbook": "BetMGM",
            "fair_probability": 0.45,
            "ev_pct": 12.5,
        }
        text = format_telegram_alert(opp)
        self.assertIn("Texas A&amp;M vs LSU", text)
        self.assertNotIn("Texas A&M", text)

    def test_send_telegram_message_success_and_failure(self):
        with patch("worker.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            self.assertTrue(send_telegram_message("test_token", "test_chat", "Hello"))

        with patch("worker.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=400, text="Bad Request")
            self.assertFalse(send_telegram_message("test_token", "test_chat", "Hello"))

        self.assertFalse(send_telegram_message("", "test_chat", "Hello"))
        self.assertFalse(send_telegram_message("test_token", "", "Hello"))

    def test_save_and_load_alert_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "alert_history.json")
            data = {"game_1": {"ev": 4.5, "timestamp": 123456.7}}
            save_alert_history(data, file_path)
            loaded = load_alert_history(file_path)
            self.assertEqual(loaded, data)

    def test_save_and_load_cached_odds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "cached_odds.json")
            quotes = [{"event_id": "1", "odds_decimal": 2.0}]
            opps = [{"event_id": "1", "ev_pct": 5.0}]
            now = datetime.now(timezone.utc)
            save_cached_odds(quotes, opps, ["NBA"], now, file_path)

            loaded = load_cached_odds(file_path)
            self.assertIsNotNone(loaded)
            self.assertEqual(len(loaded["quotes"]), 1)
            self.assertEqual(len(loaded["opportunities"]), 1)
            self.assertEqual(loaded["sports"], ["NBA"])

    def test_run_worker_e2e_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hist_file = os.path.join(tmpdir, "alert_history.json")
            cache_file = os.path.join(tmpdir, "cached_odds.json")

            future_kickoff = (
                datetime.now(timezone.utc) + timedelta(hours=3)
            ).isoformat()
            past_kickoff = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat()

            mock_quotes = [
                {"event_id": "event_1", "kickoff": future_kickoff},
                {"event_id": "event_2", "kickoff": past_kickoff},
            ]

            mock_opps = [
                {
                    "event_id": "event_1",
                    "sport": "NBA",
                    "matchup": "Team A vs Team B",
                    "market": "Spread -2.5",
                    "selection": "Team A",
                    "line": -2.5,
                    "sportsbook": "FanDuel",
                    "odds_american": -110,
                    "odds_decimal": 1.91,
                    "fair_probability": 0.56,
                    "ev_pct": 4.5,
                    "kickoff": future_kickoff,
                },
                {
                    "event_id": "event_2",
                    "sport": "NBA",
                    "matchup": "Team C vs Team D",
                    "market": "Moneyline",
                    "selection": "Team C",
                    "sportsbook": "DraftKings",
                    "ev_pct": 5.0,
                    "kickoff": past_kickoff,  # Started -> should be filtered
                },
                {
                    "event_id": "event_3",
                    "sport": "NBA",
                    "matchup": "Team E vs Team F",
                    "market": "Total 215.5",
                    "selection": "Over",
                    "sportsbook": "BetMGM",
                    "ev_pct": 2.2,  # Below 3.0% -> should be filtered
                    "kickoff": future_kickoff,
                },
            ]

            with patch("worker.run_api") as mock_run_api, patch(
                "worker.scan_pinnacle_ev"
            ) as mock_scan, patch("worker.send_telegram_message") as mock_send:
                mock_run_api.return_value = {
                    "quotes": mock_quotes,
                    "errors": [],
                    "warnings": [],
                }
                mock_scan.return_value = {"opportunities": mock_opps}
                mock_send.return_value = True

                with patch.dict(
                    os.environ,
                    {
                        "PROPLINE_API_KEY": "test_api_key",
                        "TELEGRAM_BOT_TOKEN": "bot_token",
                        "TELEGRAM_CHAT_ID": "chat_id",
                    },
                ):
                    result = run_worker(
                        sports=["NBA"],
                        min_ev=3.0,
                        history_file=hist_file,
                        cache_file=cache_file,
                    )

            self.assertTrue(result["success"])
            self.assertEqual(result["eligible_opportunities"], 1)
            self.assertEqual(result["alerts_sent"], 1)
            mock_send.assert_called_once()

            # Second run with same data: should NOT re-alert
            with patch("worker.run_api") as mock_run_api, patch(
                "worker.scan_pinnacle_ev"
            ) as mock_scan, patch("worker.send_telegram_message") as mock_send:
                mock_run_api.return_value = {
                    "quotes": mock_quotes,
                    "errors": [],
                    "warnings": [],
                }
                mock_scan.return_value = {"opportunities": mock_opps}

                with patch.dict(
                    os.environ,
                    {
                        "PROPLINE_API_KEY": "test_api_key",
                        "TELEGRAM_BOT_TOKEN": "bot_token",
                        "TELEGRAM_CHAT_ID": "chat_id",
                    },
                ):
                    result2 = run_worker(
                        sports=["NBA"],
                        min_ev=3.0,
                        history_file=hist_file,
                        cache_file=cache_file,
                    )

            self.assertTrue(result2["success"])
            self.assertEqual(result2["alerts_sent"], 0)
            mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
