import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import requests

from propline_api import (
    APPROVED_BOOKMAKERS,
    EASTERN,
    _event_is_in_mode,
    american_to_decimal,
    parse_odds_payload,
    run_api,
)


class PropLineApiTests(unittest.TestCase):
    def test_north_carolina_bookmaker_allowlist_is_exact(self):
        self.assertEqual(
            APPROVED_BOOKMAKERS,
            {
                "draftkings": "DraftKings",
                "fanduel": "FanDuel",
                "betmgm": "BetMGM",
                "fanatics": "Fanatics",
                "thescorebet": "theScore Bet",
                "underdog": "Underdog",
                "polymarket": "Polymarket",
                "polymarket_us": "Polymarket US",
                "kalshi": "Kalshi",
                "novig": "Novig",
                "prophetx": "ProphetX",
            },
        )

    def test_american_prices_and_invalid_values(self):
        self.assertAlmostEqual(american_to_decimal(-110), 1.9090909)
        self.assertEqual(american_to_decimal(150), 2.5)
        self.assertIsNone(american_to_decimal(0))
        self.assertIsNone(american_to_decimal("110"))

    def test_prediction_market_prices_are_american_and_keep_quote_liquidity(self):
        prediction_books = ("polymarket", "polymarket_us", "kalshi")
        events = [
            {
                "id": f"event-{bookmaker}",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": bookmaker,
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": 125,
                                        "liquidity": 40.5,
                                        "liquidity_updated_at": "2026-11-13T10:00:00Z",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            for bookmaker in prediction_books
        ]

        quotes = parse_odds_payload(events, "NFL")

        self.assertEqual(len(quotes), len(prediction_books))
        for quote in quotes:
            self.assertEqual(quote["selection"], "Home")
            self.assertEqual(quote["odds_american"], 125)
            self.assertEqual(quote["price_format"], "american")
            self.assertEqual(quote["odds_decimal"], 2.25)
            self.assertEqual(quote["liquidity"], 40.5)
            self.assertEqual(
                quote["liquidity_updated_at"], "2026-11-13T10:00:00Z"
            )

    def test_underdog_price_is_not_adjusted_again_by_payout_multiplier(self):
        events = [
            {
                "id": "event-underdog",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "underdog",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": 150,
                                        "payout_multiplier": 2.5,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        quote = parse_odds_payload(events, "NFL")[0]

        self.assertEqual(quote["odds_american"], 150)
        self.assertEqual(quote["odds_decimal"], 2.5)
        self.assertEqual(quote["payout_multiplier"], 2.5)

    def test_schedule_filter_uses_current_eastern_time(self):
        now = datetime(2026, 11, 13, 16, 18, tzinfo=EASTERN)
        past_same_day = {
            "commence_time": "2026-11-13T21:17:00Z",
            "live": False,
        }
        future_same_day = {
            "commence_time": "2026-11-13T21:19:00Z",
            "live": False,
        }
        future_next_day = {
            "commence_time": "2026-11-14T15:00:00Z",
            "live": False,
        }

        self.assertFalse(_event_is_in_mode(past_same_day, "upcoming", now))
        self.assertTrue(_event_is_in_mode(future_same_day, "upcoming", now))
        self.assertTrue(_event_is_in_mode(future_next_day, "upcoming", now))
        self.assertFalse(_event_is_in_mode(future_same_day, "live", now))
        self.assertTrue(
            _event_is_in_mode(
                {"commence_time": "2026-11-13T20:00:00Z", "live": True},
                "live",
                now,
            )
        )
        self.assertFalse(
            _event_is_in_mode(
                {
                    "commence_time": "2026-11-13T20:00:00Z",
                    "live": True,
                    "completed": True,
                },
                "live",
                now,
            )
        )

    def test_parser_keeps_alternate_lines_separate(self):
        events = [
            {
                "id": "event-1",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "side": "home", "price": -110},
                                    {"name": "Away", "side": "away", "price": 100},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -110, "point": 44.5},
                                    {"name": "Under", "price": -110, "point": 44.5},
                                ],
                            },
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NFL")

        self.assertEqual(len(quotes), 4)
        self.assertEqual(quotes[0]["market_key"], "h2h")
        self.assertEqual(quotes[2]["market_key"], "totals:44.5")
        self.assertEqual(quotes[2]["expected_outcomes"], ["Over", "Under"])
        self.assertAlmostEqual(quotes[0]["odds_decimal"], 1.9090909)

    def test_parser_keeps_game_and_team_totals_separate(self):
        events = [
            {
                "id": "event-totals",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": side, "price": -110, "point": 110.5}
                                    for side in ("Over", "Under")
                                ],
                            },
                            {
                                "key": "totals",
                                "team": "Home",
                                "outcomes": [
                                    {"name": side, "price": price, "point": 110.5}
                                    for side, price in (("Over", -200), ("Under", 170))
                                ],
                            },
                            {
                                "key": "totals",
                                "team": "Away",
                                "outcomes": [
                                    {"name": side, "price": price, "point": 110.5}
                                    for side, price in (("Over", 150), ("Under", -170))
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
        quotes = parse_odds_payload(events, "NBA")

        totals = {quote["market_key"]: quote for quote in quotes}
        self.assertEqual(
            set(totals),
            {"totals:110.5", "totals:110.5:team:home", "totals:110.5:team:away"},
        )
        self.assertEqual(
            totals["totals:110.5:team:home"]["market"], "Total 110.5 (Home team)"
        )
        self.assertEqual(
            totals["totals:110.5:team:away"]["market"], "Total 110.5 (Away team)"
        )

    def test_parser_uses_freshest_duplicate_market_row(self):
        events = [
            {
                "id": "event-freshness",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "spreads",
                                "last_update": "2026-11-13T10:00:00Z",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": 3300,
                                        "point": -3.5,
                                        "last_seen_at": "2026-11-13T10:00:00Z",
                                    },
                                    {
                                        "name": "Away",
                                        "side": "away",
                                        "price": 3300,
                                        "point": 3.5,
                                        "last_seen_at": "2026-11-13T10:00:00Z",
                                    },
                                ],
                            },
                            {
                                "key": "spreads",
                                "last_update": "2026-11-13T10:05:00Z",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": -110,
                                        "point": -3.5,
                                        "last_seen_at": "2026-11-13T10:05:00Z",
                                    },
                                    {
                                        "name": "Away",
                                        "side": "away",
                                        "price": -110,
                                        "point": 3.5,
                                        "last_seen_at": "2026-11-13T10:05:00Z",
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NBA")

        self.assertEqual(len(quotes), 2)
        self.assertTrue(all(quote["odds_decimal"] < 2 for quote in quotes))
        self.assertTrue(
            all(quote["last_update"] == "2026-11-13T10:05:00Z" for quote in quotes)
        )

    def test_parser_drops_outcome_missing_from_latest_market_delivery(self):
        events = [
            {
                "id": "event-withdrawn",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-11-13T10:05:00Z",
                                "outcomes": [
                                    {
                                        "name": "Home",
                                        "side": "home",
                                        "price": 110,
                                        "last_seen_at": "2026-11-13T10:00:00Z",
                                    },
                                    {
                                        "name": "Away",
                                        "side": "away",
                                        "price": -120,
                                        "last_seen_at": "2026-11-13T10:05:00Z",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        quotes = parse_odds_payload(events, "NBA")

        self.assertEqual([quote["selection"] for quote in quotes], ["Away"])

    def test_parser_keeps_only_approved_books_and_uses_team_side(self):
        events = [
            {
                "id": "event-2",
                "home_team": "Home",
                "away_team": "Away",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home alias", "side": "home", "price": 120},
                                    {"name": "Away alias", "side": "away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "bovada",
                        "title": "Bovada",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "betrivers",
                        "title": "BetRivers",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "thescorebet",
                        "title": "theScore Bet",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "underdog",
                        "title": "Underdog",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "polymarket",
                        "title": "Polymarket",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "polymarket_us",
                        "title": "Polymarket US",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "kalshi",
                        "title": "Kalshi",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "novig",
                        "title": "Novig",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "prophetx",
                        "title": "ProphetX",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "hardrock",
                        "title": "Hard Rock",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        for bookmaker_key, title in (
            ("fanduel", "FanDuel"),
            ("betmgm", "BetMGM"),
            ("fanatics", "Fanatics"),
        ):
            events[0]["bookmakers"].append(
                {
                    "key": bookmaker_key,
                    "title": title,
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Home", "price": 120},
                                {"name": "Away", "price": -130},
                            ],
                        }
                    ],
                }
            )

        quotes = parse_odds_payload(events, "NBA")

        self.assertEqual(APPROVED_BOOKMAKERS["draftkings"], "DraftKings")
        self.assertEqual(
            {quote["sportsbook"] for quote in quotes},
            {
                "DraftKings",
                "FanDuel",
                "BetMGM",
                "Fanatics",
                "theScore Bet",
                "Underdog",
                "Polymarket",
                "Polymarket US",
                "Kalshi",
                "Novig",
                "ProphetX",
            },
        )
        self.assertEqual({quote["selection"] for quote in quotes}, {"Home", "Away"})

    def test_parser_includes_pinnacle_without_mixing_three_way_books(self):
        events = [
            {
                "id": "event-pinnacle",
                "home_team": "Home",
                "away_team": "Away",
                "commence_time": "2026-11-14T15:00:00Z",
                "bookmakers": [
                    {
                        "key": "pinnacle",
                        "title": "Pinnacle",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-11-13T10:00:00Z",
                                "outcomes": [
                                    {"name": "Home", "price": -110},
                                    {"name": "Away", "price": -110},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 140},
                                    {"name": "Away", "price": 130},
                                    {"name": "Draw", "price": 220},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

        quotes = parse_odds_payload(events, "Soccer")
        pinnacle_quotes = [
            item for item in quotes if item["bookmaker_key"] == "pinnacle"
        ]
        draftkings_quotes = [
            item for item in quotes if item["bookmaker_key"] == "draftkings"
        ]

        self.assertEqual(len(pinnacle_quotes), 2)
        self.assertTrue(
            all(item["expected_outcomes"] == ["Home", "Away"] for item in pinnacle_quotes)
        )
        self.assertTrue(
            all("Draw" in item["expected_outcomes"] for item in draftkings_quotes)
        )
        self.assertEqual({item["odds_american"] for item in pinnacle_quotes}, {-110})

    def test_bulk_scan_uses_one_request_for_selected_sport(self):
        response = Mock()
        response.status_code = 200
        response.headers = {
            "X-Daily-Remaining": "997",
            "X-Daily-Limit": "1000",
        }
        response.json.return_value = [
            {
                "id": "live-game",
                "home_team": "Home",
                "away_team": "Away",
                "live": True,
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Home", "price": 120},
                                    {"name": "Away", "price": -130},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        session = Mock()
        session.get.return_value = response
        with patch("propline_api.requests.Session", return_value=session):
            result = run_api(["NBA"], "live", "test-key")

        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["quota"]["X-Daily-Remaining"], "997")
        self.assertEqual(len(result["quotes"]), 2)
        session.get.assert_called_once()
        self.assertEqual(
            session.get.call_args.kwargs["params"],
            {
                "markets": "h2h,spreads,totals",
                "bookmakers": ",".join(
                    [*APPROVED_BOOKMAKERS, "pinnacle"]
                ),
                "max_age": 60,
            },
        )
        self.assertEqual(
            session.get.call_args.kwargs["headers"], {"X-API-Key": "test-key"}
        )
        session.close.assert_called_once()

    def test_transient_tls_handshake_gets_two_retries(self):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = []
        initial_session = Mock()
        initial_session.get.side_effect = requests.exceptions.SSLError(
            "transient TLS failure"
        )
        retry_session = Mock()
        retry_session.get.side_effect = requests.exceptions.SSLError(
            "second transient TLS failure"
        )
        final_session = Mock()
        final_session.get.return_value = response

        with (
            patch(
                "propline_api.requests.Session",
                side_effect=[initial_session, retry_session, final_session],
            ) as make_session,
            patch("propline_api.time.sleep") as sleep,
        ):
            result = run_api(["NBA"], "live", "test-key")

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(make_session.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        initial_session.close.assert_called_once()
        retry_session.close.assert_called_once()
        retry_session.get.assert_called_once()
        final_session.get.assert_called_once()
        final_session.close.assert_called_once()

    def test_read_timeout_retries_once_with_a_fresh_session(self):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = []
        initial_session = Mock()
        initial_session.get.side_effect = requests.exceptions.ReadTimeout(
            "read timed out"
        )
        retry_session = Mock()
        retry_session.get.return_value = response

        with (
            patch(
                "propline_api.requests.Session",
                side_effect=[initial_session, retry_session],
            ) as make_session,
            patch("propline_api.time.sleep") as sleep,
        ):
            result = run_api(["NCAAF"], "live", "test-key")

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(make_session.call_count, 2)
        self.assertEqual(sleep.call_args.args, (0.5,))
        initial_session.get.assert_called_once()
        retry_session.get.assert_called_once()
        initial_session.close.assert_called_once()
        retry_session.close.assert_called_once()

    def test_read_timeout_reports_after_retry_exhausted(self):
        initial_session = Mock()
        retry_session = Mock()
        initial_session.get.side_effect = requests.exceptions.ReadTimeout(
            "first read timed out"
        )
        retry_session.get.side_effect = requests.exceptions.ReadTimeout(
            "second read timed out"
        )

        with (
            patch(
                "propline_api.requests.Session",
                side_effect=[initial_session, retry_session],
            ),
            patch("propline_api.time.sleep"),
        ):
            result = run_api(["NCAAF"], "live", "test-key")

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("request timed out after 2 attempt(s)", result["errors"][0])
        self.assertIn("NCAAF", result["errors"][0])
        initial_session.close.assert_called_once()
        retry_session.close.assert_called_once()

    def test_batch_continues_to_next_sport_after_tls_failure(self):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = []
        first_session = Mock()
        second_session = Mock()
        third_session = Mock()
        fourth_session = Mock()
        fifth_session = Mock()
        sixth_session = Mock()
        first_session.get.side_effect = requests.exceptions.SSLError(
            "transient TLS failure"
        )
        second_session.get.side_effect = requests.exceptions.SSLError(
            "retry TLS failure"
        )
        third_session.get.side_effect = requests.exceptions.SSLError(
            "final TLS failure"
        )
        fourth_session.get.side_effect = requests.exceptions.SSLError(
            "fourth TLS failure"
        )
        fifth_session.get.side_effect = requests.exceptions.SSLError(
            "fifth TLS failure"
        )
        sixth_session.get.return_value = response

        with (
            patch(
                "propline_api.requests.Session",
                side_effect=[
                    first_session,
                    second_session,
                    third_session,
                    fourth_session,
                    fifth_session,
                    sixth_session,
                ],
            ) as make_session,
            patch("propline_api.time.sleep"),
        ):
            result = run_api(["NBA", "NHL"], "live", "test-key")

        self.assertEqual(result["request_count"], 2)
        self.assertEqual(make_session.call_count, 6)
        self.assertIn("NBA: TLS handshake failed after 5 attempts", result["errors"][0])
        self.assertEqual(sixth_session.get.call_count, 1)
        first_session.close.assert_called_once()
        second_session.close.assert_called_once()
        third_session.close.assert_called_once()
        fourth_session.close.assert_called_once()
        fifth_session.close.assert_called_once()
        sixth_session.close.assert_called_once()

    def test_certificate_validation_error_is_not_retried(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError(
            "certificate verify failed"
        )

        with (
            patch("propline_api.requests.Session", return_value=session) as make_session,
            patch("propline_api.time.sleep") as sleep,
        ):
            result = run_api(["NBA"], "live", "test-key")

        self.assertEqual(result["request_count"], 1)
        self.assertIn("TLS certificate validation failed", result["errors"][0])
        session.get.assert_called_once()
        make_session.assert_called_once()
        sleep.assert_not_called()
        session.close.assert_called_once()

    def test_missing_key_does_not_make_a_request(self):
        with patch("propline_api.requests.Session") as make_session:
            result = run_api(["NBA"], api_key="")

        self.assertEqual(result["request_count"], 0)
        self.assertTrue(result["errors"])
        make_session.assert_not_called()

    def test_rate_limit_is_reported_without_retrying_requests(self):
        response = Mock()
        response.status_code = 429
        response.headers = {
            "Retry-After": "30",
            "X-Daily-Remaining": "0",
            "X-Daily-Limit": "1000",
        }

        session = Mock()
        session.get.return_value = response
        with patch("propline_api.requests.Session", return_value=session):
            result = run_api(["NBA", "NHL"], "live", "test-key")

        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["quota"]["X-Daily-Remaining"], "0")
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(all("No automatic retry" in error for error in result["errors"]))
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
