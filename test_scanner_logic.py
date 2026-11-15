import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from scanner_config import CONFIG
from ev_engine import calculate_capped_kelly_stakes
from scanner_logic import (
    devig_two_way,
    estimate_line_adjusted_probability,
    expected_value_dollars,
    expected_value_pct,
    find_arbitrage_opportunities,
    scan_pinnacle_ev,
)


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FRESH_QUOTE_TIME = "2026-01-01T11:59:30Z"
KICKOFF = "2026-01-01T13:00:00Z"


def quote(
    bookmaker,
    selection,
    decimal_odds,
    *,
    market_key="h2h",
    period="",
    point=None,
    team=None,
    expected=None,
    quote_time=FRESH_QUOTE_TIME,
    kickoff=KICKOFF,
):
    is_pinnacle = bookmaker == "Pinnacle"
    return {
        "sport": "NBA",
        "event_id": "game-1",
        "matchup": "Home vs Away",
        "kickoff": kickoff,
        "market": market_key,
        "market_key": market_key,
        "period": period,
        "team": team,
        "point": point,
        "selection": selection,
        "expected_outcomes": expected or ["Home", "Away"],
        "sportsbook": bookmaker,
        "bookmaker_key": "pinnacle" if is_pinnacle else bookmaker.casefold(),
        "odds_decimal": decimal_odds,
        "odds_american": 100 if decimal_odds >= 2 else -110,
        "last_seen_at": quote_time,
    }


def two_way_quotes(
    *,
    market_key="h2h",
    period="",
    point_a=None,
    point_b=None,
    team=None,
    pin_a=1.9090909091,
    pin_b=1.9090909091,
    book_a=2.1,
    book_b=1.8,
    expected=None,
    quote_time=FRESH_QUOTE_TIME,
    kickoff=KICKOFF,
):
    outcomes = expected or ["Home", "Away"]
    return [
        quote(
            "Pinnacle",
            outcomes[0],
            pin_a,
            market_key=market_key,
            period=period,
            point=point_a,
            team=team,
            expected=outcomes,
            quote_time=quote_time,
            kickoff=kickoff,
        ),
        quote(
            "Pinnacle",
            outcomes[1],
            pin_b,
            market_key=market_key,
            period=period,
            point=point_b,
            team=team,
            expected=outcomes,
            quote_time=quote_time,
            kickoff=kickoff,
        ),
        quote(
            "DraftKings",
            outcomes[0],
            book_a,
            market_key=market_key,
            period=period,
            point=point_a,
            team=team,
            expected=outcomes,
            quote_time=quote_time,
            kickoff=kickoff,
        ),
        quote(
            "FanDuel",
            outcomes[1],
            book_b,
            market_key=market_key,
            period=period,
            point=point_b,
            team=team,
            expected=outcomes,
            quote_time=quote_time,
            kickoff=kickoff,
        ),
    ]


def pinnacle_spread_line(point, fair_probability, *, sport="NBA", event_id="game-1"):
    home_decimal = 1 / fair_probability
    away_decimal = 1 / (1 - fair_probability)
    home = quote(
        "Pinnacle",
        "Home",
        home_decimal,
        market_key=f"spreads:{point}",
        point=point,
        expected=["Home", "Away"],
    )
    away = quote(
        "Pinnacle",
        "Away",
        away_decimal,
        market_key=f"spreads:{-point}",
        point=-point,
        expected=["Home", "Away"],
    )
    home["sport"] = sport
    away["sport"] = sport
    home["event_id"] = event_id
    away["event_id"] = event_id
    return [home, away]


def pinnacle_total_line(point, fair_over, *, sport="NBA", event_id="game-1"):
    over = quote(
        "Pinnacle",
        "Over",
        1 / fair_over,
        market_key=f"totals:{point}",
        point=point,
        expected=["Over", "Under"],
    )
    under = quote(
        "Pinnacle",
        "Under",
        1 / (1 - fair_over),
        market_key=f"totals:{point}",
        point=point,
        expected=["Over", "Under"],
    )
    over["sport"] = sport
    under["sport"] = sport
    over["event_id"] = event_id
    under["event_id"] = event_id
    return [over, under]


class PinnacleFairProbabilityTests(unittest.TestCase):
    def test_minus_110_prices_devig_to_fifty_fifty(self):
        fair_a, fair_b = devig_two_way(1 + 100 / 110, 1 + 100 / 110)
        self.assertAlmostEqual(fair_a, 0.5, places=12)
        self.assertAlmostEqual(fair_b, 0.5, places=12)

    def test_power_fair_probabilities_sum_to_one(self):
        for prices in ((1.91, 1.91), (2.5, 1.6), (11.0, 1.06)):
            fair_a, fair_b = devig_two_way(*prices, method="power")
            self.assertAlmostEqual(fair_a + fair_b, 1.0, delta=1e-9)
        shin_fair = devig_two_way(2.5, 1.6, method="shin")
        self.assertAlmostEqual(sum(shin_fair), 1.0, delta=1e-9)

    def test_ev_formula(self):
        self.assertAlmostEqual(expected_value_pct(2.1, 0.5), 5.0)
        self.assertAlmostEqual(expected_value_dollars(2.1, 0.5, 100), 5.0)

    def test_power_devig_reduces_lopsided_longshot_probability(self):
        power_longshot = devig_two_way(11.0, 1.06, "power")[0]
        proportional_longshot = devig_two_way(11.0, 1.06, "multiplicative")[0]
        self.assertLess(power_longshot, proportional_longshot)

    def test_spread_with_two_identical_signed_points_is_rejected(self):
        quotes = two_way_quotes(
            market_key="spreads:23.5",
            point_a=-23.5,
            point_b=-23.5,
            expected=["Home", "Away"],
        )
        result = scan_pinnacle_ev(quotes, now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["line_mismatch"], 2)

    def test_one_sided_pinnacle_market_is_counted_and_rejected(self):
        quotes = two_way_quotes()
        quotes.remove(next(item for item in quotes if item["bookmaker_key"] == "pinnacle" and item["selection"] == "Away"))
        result = scan_pinnacle_ev(quotes, now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(
            result["diagnostics"]["dropped"]["one_sided_pinnacle_market"], 2
        )

    def test_missing_pinnacle_reference_does_not_fall_back_to_soft_books(self):
        only_books = [
            quote("DraftKings", "Home", 10.0),
            quote("FanDuel", "Away", 1.05),
        ]
        result = scan_pinnacle_ev(only_books, now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(
            result["diagnostics"]["dropped"]["no_pinnacle_reference"], 2
        )

    def test_bookmaker_three_way_price_does_not_match_pinnacle_two_way_market(self):
        pinnacle_pair = two_way_quotes()[:2]
        three_way_price = quote(
            "DraftKings",
            "Home",
            3.0,
            expected=["Home", "Away", "Draw"],
        )
        result = scan_pinnacle_ev(
            pinnacle_pair + [three_way_price], now=NOW
        )
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["line_mismatch"], 1)

    def test_period_and_team_scope_do_not_match_full_game_total(self):
        pin_full_game = two_way_quotes(
            market_key="totals:220.5",
            point_a=220.5,
            point_b=220.5,
            expected=["Over", "Under"],
            book_a=2.1,
            book_b=1.8,
        )[:2]
        first_half_book = quote(
            "DraftKings",
            "Over",
            2.1,
            market_key="totals:220.5",
            period="first_half",
            point=220.5,
            expected=["Over", "Under"],
        )
        period_result = scan_pinnacle_ev(pin_full_game + [first_half_book], now=NOW)
        self.assertEqual(
            period_result["diagnostics"]["dropped"]["period_mismatch"], 1
        )
        self.assertEqual(period_result["opportunities"], [])

        team_scope_book = quote(
            "DraftKings",
            "Over",
            2.1,
            market_key="totals:220.5:team:home",
            point=220.5,
            team="Home",
            expected=["Over", "Under"],
        )
        team_result = scan_pinnacle_ev(pin_full_game + [team_scope_book], now=NOW)
        self.assertEqual(
            team_result["diagnostics"]["dropped"]["team_scope_mismatch"], 1
        )
        self.assertEqual(team_result["opportunities"], [])

    def test_pinnacle_is_reference_only_and_large_edges_are_flagged(self):
        result = scan_pinnacle_ev(
            two_way_quotes(book_a=2.4, book_b=1.8), now=NOW
        )
        self.assertEqual(len(result["opportunities"]), 1)
        row = result["opportunities"][0]
        self.assertEqual(row["sportsbook"], "DraftKings")
        self.assertEqual(row["reference"], "Pinnacle")
        self.assertEqual(row["flags"], "verify")
        self.assertAlmostEqual(row["fair_probability"], 0.5, places=6)
        self.assertEqual(row["pinnacle_american_this_side"], -110)
        self.assertEqual(row["pinnacle_american_other_side"], -110)

    def test_missing_quote_timestamps_do_not_cause_stale_drops(self):
        result = scan_pinnacle_ev(
            two_way_quotes(book_a=2.4, quote_time=None), now=NOW
        )
        self.assertTrue(result["opportunities"])
        self.assertEqual(result["diagnostics"]["dropped"]["stale_quote"], 0)

    def test_started_event_is_dropped(self):
        result = scan_pinnacle_ev(
            two_way_quotes(kickoff="2026-01-01T11:00:00Z"), now=NOW
        )
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["started"], 2)

    def test_same_fair_probability_is_applied_to_each_bettable_book(self):
        reference = two_way_quotes(book_a=2.2, book_b=2.0)[:2]
        draftkings_result = scan_pinnacle_ev(
            reference + [quote("DraftKings", "Home", 2.2)], now=NOW
        )
        betmgm_result = scan_pinnacle_ev(
            reference + [quote("BetMGM", "Home", 2.3)], now=NOW
        )
        self.assertEqual(draftkings_result["opportunities"][0]["sportsbook"], "DraftKings")
        self.assertEqual(betmgm_result["opportunities"][0]["sportsbook"], "BetMGM")
        self.assertEqual(
            draftkings_result["opportunities"][0]["fair_probability"],
            betmgm_result["opportunities"][0]["fair_probability"],
        )

    def test_minimum_liquidity_filters_only_reported_values_below_threshold(self):
        config = replace(CONFIG, min_liquidity_dollars=100.0)
        quotes = two_way_quotes(book_a=2.5)
        pinnacle_quotes = quotes[:2]
        low_liquidity_quote = quotes[2]
        low_liquidity_quote["liquidity"] = 99.99

        filtered = scan_pinnacle_ev(
            pinnacle_quotes + [low_liquidity_quote], config=config, now=NOW
        )
        self.assertEqual(filtered["opportunities"], [])
        self.assertEqual(filtered["diagnostics"]["dropped"]["below_min_liquidity"], 1)

        for liquidity in (None, 100.0):
            with self.subTest(liquidity=liquidity):
                eligible_quote = quote("DraftKings", "Home", 2.5)
                eligible_quote["liquidity"] = liquidity
                result = scan_pinnacle_ev(
                    pinnacle_quotes + [eligible_quote], config=config, now=NOW
                )
                self.assertEqual(len(result["opportunities"]), 1)
                self.assertEqual(
                    result["diagnostics"]["dropped"]["below_min_liquidity"], 0
                )

    def test_alt_line_ladder_keeps_only_the_highest_ev_selection_rung(self):
        quotes = []
        for line, book_odds in ((1.5, 2.5), (2.5, 2.1)):
            quotes.extend(
                two_way_quotes(
                    market_key=f"spreads:{line}",
                    point_a=-line,
                    point_b=line,
                    book_a=book_odds,
                    book_b=1.8,
                )
            )
        result = scan_pinnacle_ev(quotes, now=NOW)
        home_rows = [
            row
            for row in result["opportunities"]
            if row["selection"] == "Home"
        ]
        self.assertEqual(len(home_rows), 1)
        self.assertEqual(home_rows[0]["point"], -1.5)
        self.assertEqual(result["diagnostics"]["dropped"]["alt_line_duplicate"], 1)

    def test_stale_and_high_overround_references_are_dropped(self):
        stale = two_way_quotes(
            quote_time="2026-01-01T11:00:00Z", book_a=2.4
        )
        stale_result = scan_pinnacle_ev(stale, now=NOW)
        self.assertEqual(stale_result["opportunities"], [])
        self.assertEqual(stale_result["diagnostics"]["dropped"]["stale_quote"], 2)

        high_overround = two_way_quotes(
            pin_a=1.5, pin_b=1.5, book_a=2.4
        )
        overround_result = scan_pinnacle_ev(high_overround, now=NOW)
        self.assertEqual(overround_result["opportunities"], [])
        self.assertEqual(
            overround_result["diagnostics"]["dropped"]["overround_too_high"], 2
        )

    def test_line_adjustment_is_disabled_by_default(self):
        references = pinnacle_spread_line(-2.5, 0.55)
        venue = quote(
            "DraftKings",
            "Home",
            2.1,
            market_key="spreads:-3.5",
            point=-3.5,
            expected=["Home", "Away"],
        )
        result = scan_pinnacle_ev(references + [venue], now=NOW)
        self.assertFalse(CONFIG.enable_line_adjusted)
        self.assertEqual(result["opportunities"], [])

    def test_exact_pinnacle_line_takes_precedence_when_line_adjustment_is_enabled(self):
        references = (
            pinnacle_spread_line(-2.5, 0.6)
            + pinnacle_spread_line(-4.5, 0.4)
            + pinnacle_spread_line(-3.5, 0.52)
        )
        venue = quote(
            "DraftKings",
            "Home",
            2.4,
            market_key="spreads:-3.5",
            point=-3.5,
            expected=["Home", "Away"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertEqual(row["pricing_method"], "exact")
        self.assertAlmostEqual(row["fair_probability"], 0.52)

    def test_line_adjustment_interpolates_between_pinnacle_lines(self):
        references = (
            pinnacle_spread_line(-2.5, 0.6)
            + pinnacle_spread_line(-4.5, 0.4)
        )
        venue = quote(
            "DraftKings",
            "Home",
            2.4,
            market_key="spreads:-3.5",
            point=-3.5,
            expected=["Home", "Away"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertAlmostEqual(row["fair_probability"], 0.5)
        self.assertEqual(row["pricing_method"], "line-adjusted")
        self.assertEqual(row["line_adjustment_method"], "interpolation")
        self.assertEqual(row["flags"], "line-adjusted, verify")
        self.assertEqual(row["pinnacle_lines_used"], "-4.5, -2.5")
        self.assertEqual(row["line"], -3.5)
        self.assertEqual(
            row["kelly_fraction"],
            CONFIG.effective_line_adjusted_kelly_fraction,
        )
        stake = calculate_capped_kelly_stakes(
            [row],
            bankroll=10000,
            fraction=0.25,
            per_bet_cap_fraction=1,
            per_event_cap_fraction=1,
        )[0]
        self.assertAlmostEqual(
            stake,
            round(
                10000
                * ((0.5 * 2.4 - 1) / (2.4 - 1))
                * CONFIG.effective_line_adjusted_kelly_fraction,
                2,
            ),
        )
        self.assertAlmostEqual(row["ev_pct"], 20.0)

    def test_line_adjustment_uses_distribution_when_no_lines_bracket_target(self):
        references = pinnacle_spread_line(-2.5, 0.5, sport="NFL")
        venue = quote(
            "DraftKings",
            "Home",
            2.4,
            market_key="spreads:-3.5",
            point=-3.5,
            expected=["Home", "Away"],
        )
        venue["sport"] = "NFL"
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertEqual(row["line_adjustment_method"], "distribution")
        self.assertLess(row["fair_probability"], 0.5)

    def test_total_line_interpolation_preserves_over_under_direction(self):
        references = (
            pinnacle_total_line(200.5, 0.6)
            + pinnacle_total_line(202.5, 0.4)
        )
        venue = quote(
            "DraftKings",
            "Over",
            2.4,
            market_key="totals:201.5",
            point=201.5,
            expected=["Over", "Under"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertAlmostEqual(row["fair_probability"], 0.5)
        self.assertEqual(row["line_adjustment_method"], "interpolation")

    def test_line_adjustment_does_not_extrapolate_past_max_line_difference(self):
        references = (
            pinnacle_spread_line(-2.5, 0.6)
            + pinnacle_spread_line(-4.5, 0.4)
        )
        venue = quote(
            "DraftKings",
            "Home",
            2.4,
            market_key="spreads:-6.5",
            point=-6.5,
            expected=["Home", "Away"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["diagnostics"]["dropped"]["line_mismatch"], 1)

    def test_football_key_number_crossing_has_extra_probability_effect(self):
        crossing = estimate_line_adjusted_probability(
            "NFL",
            "spreads",
            "Home",
            -3.5,
            [{"point": -2.5, "fair_probability": 0.5, "overround": 1.04}],
        )
        ordinary = estimate_line_adjusted_probability(
            "NFL",
            "spreads",
            "Home",
            -5.5,
            [{"point": -4.5, "fair_probability": 0.5, "overround": 1.04}],
        )
        self.assertGreater(
            abs(crossing["fair_probability"] - 0.5),
            abs(ordinary["fair_probability"] - 0.5),
        )

    def test_line_adjustment_never_crosses_period_or_team_scope(self):
        config = replace(CONFIG, enable_line_adjusted=True)
        full_game = two_way_quotes(
            market_key="totals:220.5",
            point_a=220.5,
            point_b=220.5,
            expected=["Over", "Under"],
        )[:2]
        first_half = quote(
            "DraftKings",
            "Over",
            2.1,
            market_key="totals:221.5",
            period="first_half",
            point=221.5,
            expected=["Over", "Under"],
        )
        period_result = scan_pinnacle_ev(
            full_game + [first_half], config=config, now=NOW
        )
        self.assertEqual(period_result["opportunities"], [])
        self.assertEqual(
            period_result["diagnostics"]["dropped"]["period_mismatch"], 1
        )

        team_total = quote(
            "DraftKings",
            "Over",
            2.1,
            market_key="totals:221.5:team:home",
            point=221.5,
            team="Home",
            expected=["Over", "Under"],
        )
        team_result = scan_pinnacle_ev(
            full_game + [team_total], config=config, now=NOW
        )
        self.assertEqual(team_result["opportunities"], [])
        self.assertEqual(
            team_result["diagnostics"]["dropped"]["team_scope_mismatch"], 1
        )

    def test_whole_number_line_estimates_push_and_uses_push_aware_ev(self):
        exact_result = scan_pinnacle_ev(
            two_way_quotes(
                market_key="spreads:-3",
                point_a=-3,
                point_b=3,
                book_a=2.2,
            ),
            now=NOW,
        )
        exact_row = exact_result["opportunities"][0]
        self.assertEqual(exact_row["pricing_method"], "exact")
        self.assertGreater(exact_row["push_probability"], 0)

        references = pinnacle_spread_line(-2.5, 0.55)
        venue = quote(
            "DraftKings",
            "Home",
            2.2,
            market_key="spreads:-3",
            point=-3,
            expected=["Home", "Away"],
        )
        config = replace(CONFIG, enable_line_adjusted=True)
        result = scan_pinnacle_ev(references + [venue], config=config, now=NOW)
        row = result["opportunities"][0]
        self.assertGreater(row["push_probability"], 0)
        self.assertAlmostEqual(
            row["ev_pct"],
            (
                row["win_probability"] * (row["odds_decimal"] - 1)
                - row["lose_probability"]
            )
            * 100,
        )


class ExistingScannerBehaviorTests(unittest.TestCase):
    def test_arbitrage_preserves_quote_prices_and_liquidity(self):
        quotes = [
            {
                "sport": "NBA",
                "event_id": "game-1",
                "market_key": "h2h",
                "period": "",
                "matchup": "Sample",
                "market": "Moneyline",
                "selection": "Home",
                "expected_outcomes": ["Home", "Away"],
                "sportsbook": "A",
                "odds_american": 150,
                "odds_decimal": 2.5,
                "liquidity": 400,
                "liquidity_updated_at": "2026-11-13T10:00:00Z",
                "last_seen_at": "2026-11-13T10:00:00Z",
            },
            {
                "sport": "NBA",
                "event_id": "game-1",
                "market_key": "h2h",
                "period": "",
                "matchup": "Sample",
                "market": "Moneyline",
                "selection": "Away",
                "expected_outcomes": ["Home", "Away"],
                "sportsbook": "B",
                "odds_decimal": 1.7,
                "last_seen_at": "2026-11-13T10:00:00Z",
            },
        ]
        opportunities = find_arbitrage_opportunities(
            quotes,
            1000,
            rounding_step=1,
            now=datetime(2026, 11, 13, 10, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(opportunities), 1)
        arb = opportunities[0]
        self.assertEqual(sum(leg["stake"] for leg in arb["legs"]), 1000)
        self.assertGreater(arb["profit"], 0)
        self.assertAlmostEqual(arb["profit"], arb["locked_payout"] - 1000)
        first_leg = next(leg for leg in arb["legs"] if leg["sportsbook"] == "A")
        self.assertEqual(first_leg["odds_american"], 150)
        self.assertEqual(first_leg["liquidity"], 400)
        self.assertEqual(
            first_leg["liquidity_updated_at"], "2026-11-13T10:00:00Z"
        )

    def test_arbitrage_excludes_stale_quotes_and_uses_market_update_fallback(self):
        stale_time = (NOW - timedelta(seconds=CONFIG.max_quote_age_seconds + 1)).isoformat()
        stale_quotes = [
            quote("A", "Home", 2.5, quote_time=stale_time),
            quote("B", "Away", 2.0, quote_time=stale_time),
        ]
        self.assertEqual(
            find_arbitrage_opportunities(stale_quotes, 100, now=NOW),
            [],
        )

        fresh_quotes = [
            {
                **quote("A", "Home", 2.5, quote_time=None),
                "last_change_at": stale_time,
                "last_update": NOW.isoformat(),
            },
            quote("B", "Away", 2.0, quote_time=NOW.isoformat()),
        ]
        self.assertEqual(
            len(find_arbitrage_opportunities(fresh_quotes, 100, now=NOW)),
            1,
        )
        fresh_quotes[1].pop("last_seen_at")
        self.assertEqual(
            find_arbitrage_opportunities(fresh_quotes, 100, now=NOW),
            [],
        )

    def test_fractional_kelly_respects_per_bet_and_per_event_caps_to_cents(self):
        opportunities = [
            {
                "sport": "NBA",
                "event_id": "same-event",
                "fair_probability": 0.8,
                "odds_decimal": 2.0,
            }
            for _ in range(4)
        ]
        opportunities.append(
            {
                "sport": "NBA",
                "event_id": "other-event",
                "fair_probability": 0.8,
                "odds_decimal": 2.0,
            }
        )
        stakes = calculate_capped_kelly_stakes(
            opportunities, bankroll=1000, fraction=0.5
        )
        self.assertTrue(all(stake <= 10 for stake in stakes))
        self.assertLessEqual(sum(stakes[:4]), 30)
        self.assertTrue(all(round(stake, 2) == stake for stake in stakes))
        self.assertEqual(stakes[4], 10.0)


if __name__ == "__main__":
    unittest.main()
