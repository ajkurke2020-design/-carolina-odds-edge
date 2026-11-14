from collections import defaultdict
from math import ceil, isfinite
from statistics import NormalDist, median
from datetime import datetime, timezone
from typing import Any

from scanner_config import CONFIG, DEVIG_METHODS


def _expected_profit_per_dollar(
    decimal_odds: float, fair_probability: float
) -> float:
    if (
        not isfinite(decimal_odds)
        or decimal_odds <= 1
        or not isfinite(fair_probability)
        or not 0 <= fair_probability <= 1
    ):
        raise ValueError("Decimal odds must exceed 1 and probability must be between 0 and 1.")
    return (
        fair_probability * (decimal_odds - 1)
        - (1 - fair_probability)
    )


def expected_value_pct(decimal_odds: float, fair_probability: float) -> float:
    return _expected_profit_per_dollar(decimal_odds, fair_probability) * 100


def expected_value_dollars(
    decimal_odds: float, fair_probability: float, stake: float
) -> float:
    if not isfinite(stake) or stake < 0:
        raise ValueError("Stake must be a nonnegative finite amount.")
    return _expected_profit_per_dollar(decimal_odds, fair_probability) * stake


def expected_value_with_push_pct(
    decimal_odds: float, win_probability: float, lose_probability: float
) -> float:
    if (
        not isfinite(decimal_odds)
        or decimal_odds <= 1
        or not isfinite(win_probability)
        or not isfinite(lose_probability)
        or win_probability < 0
        or lose_probability < 0
        or win_probability + lose_probability > 1 + 1e-9
    ):
        raise ValueError("Odds and outcome probabilities are invalid.")
    return (
        win_probability * (decimal_odds - 1) - lose_probability
    ) * 100


def devig_two_way(
    decimal_a: float, decimal_b: float, method: str = CONFIG.devig_method
) -> tuple[float, float]:
    if (
        not isfinite(decimal_a)
        or not isfinite(decimal_b)
        or decimal_a <= 1
        or decimal_b <= 1
    ):
        raise ValueError("Both decimal prices must be finite and greater than 1.")
    if method not in DEVIG_METHODS:
        raise ValueError(f"Unsupported de-vig method: {method}.")

    implied_a = 1 / decimal_a
    implied_b = 1 / decimal_b
    overround = implied_a + implied_b
    if overround < 1 - 1e-12:
        raise ValueError("A two-way Pinnacle reference must have an overround of at least 1.")

    if method == "multiplicative":
        return implied_a / overround, implied_b / overround

    if method == "power":
        if abs(overround - 1) <= 1e-12:
            return implied_a, implied_b
        low, high = 1.0, 10.0
        if implied_a**high + implied_b**high > 1:
            raise ValueError("Power de-vig root is outside the configured range.")
        for _ in range(100):
            exponent = (low + high) / 2
            if implied_a**exponent + implied_b**exponent > 1:
                low = exponent
            else:
                high = exponent
        exponent = (low + high) / 2
        return implied_a**exponent, implied_b**exponent

    if abs(overround - 1) <= 1e-12:
        return implied_a, implied_b
    if abs(implied_a - implied_b) <= 1e-12:
        return 0.5, 0.5

    def shin_probability(implied: float, z: float) -> float:
        return (
            (z * z + 4 * (1 - z) * implied * implied / overround) ** 0.5 - z
        ) / (2 * (1 - z))

    low, high = 0.0, 1 - 1e-12
    for _ in range(100):
        z = (low + high) / 2
        fair_a = shin_probability(implied_a, z)
        fair_b = shin_probability(implied_b, z)
        if fair_a + fair_b > 1:
            low = z
        else:
            high = z
    z = (low + high) / 2
    return shin_probability(implied_a, z), shin_probability(implied_b, z)


def filter_quotes(
    quotes: list[dict[str, Any]], selected_sports: list[str], selected_books: list[str]
) -> list[dict[str, Any]]:
    return [
        quote
        for quote in quotes
        if quote["sport"] in selected_sports and quote["sportsbook"] in selected_books
    ]


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _quote_time(quote: dict[str, Any]) -> datetime | None:
    for field in ("last_seen_at", "last_update", "book_updated_at", "last_change_at"):
        parsed = _parse_utc(quote.get(field))
        if parsed is not None:
            return parsed
    return None


def _quote_time_text(quote: dict[str, Any]) -> str | None:
    timestamp = _quote_time(quote)
    return timestamp.isoformat() if timestamp else None


def _base_market(quote: dict[str, Any]) -> str:
    return str(quote.get("market_key") or "").split(":", 1)[0].casefold()


def _numeric_point(quote: dict[str, Any]) -> float | None:
    try:
        point = float(quote.get("point"))
    except (TypeError, ValueError):
        return None
    return point if isfinite(point) else None


def _reference_key(quote: dict[str, Any]) -> tuple[Any, ...] | None:
    market = _base_market(quote)
    point = _numeric_point(quote)
    if market in {"spreads", "totals"} and point is None:
        return None
    if market == "spreads":
        line = round(abs(point), 6)
    elif market == "totals":
        line = round(point, 6)
    else:
        line = None
    team_scope = (
        str(quote.get("team") or "").strip().casefold()
        if market == "totals"
        else ""
    )
    return (
        str(quote.get("sport") or ""),
        str(quote.get("event_id") or ""),
        market,
        str(quote.get("period") or ""),
        team_scope,
        line,
    )


def _selection_coordinate(market: str, selection: str, point: float) -> float | None:
    if market == "spreads":
        return point
    if market == "totals":
        normalized_selection = selection.strip().casefold()
        if normalized_selection == "over":
            return -point
        if normalized_selection == "under":
            return point
    return None


def _is_whole_line(point: float) -> bool:
    return abs(point - round(point)) <= 1e-6


def _normal_conditional_probability(
    mean: float, standard_deviation: float, whole_line: bool
) -> float:
    distribution = NormalDist(mean, standard_deviation)
    boundary = 0.5 if whole_line else 0.0
    win_probability = 1 - distribution.cdf(boundary)
    lose_probability = distribution.cdf(-boundary)
    resolved_probability = win_probability + lose_probability
    return win_probability / resolved_probability if resolved_probability else 0.5


def _normal_mean_for_probability(
    probability: float, standard_deviation: float, whole_line: bool
) -> float:
    target = min(max(probability, 1e-9), 1 - 1e-9)
    extent = max(20 * standard_deviation, 2.0 if whole_line else 0.0)
    low, high = -extent, extent
    for _ in range(100):
        mean = (low + high) / 2
        if _normal_conditional_probability(
            mean, standard_deviation, whole_line
        ) < target:
            low = mean
        else:
            high = mean
    return (low + high) / 2


def _normal_outcome_probabilities(
    probability: float, standard_deviation: float, whole_line: bool
) -> tuple[float, float, float]:
    if not whole_line:
        return probability, 1 - probability, 0.0
    mean = _normal_mean_for_probability(probability, standard_deviation, True)
    distribution = NormalDist(mean, standard_deviation)
    win_probability = 1 - distribution.cdf(0.5)
    lose_probability = distribution.cdf(-0.5)
    push_probability = max(0.0, 1 - win_probability - lose_probability)
    total = win_probability + lose_probability + push_probability
    return (
        win_probability / total,
        lose_probability / total,
        push_probability / total,
    )


def _standard_deviation_for_sport(sport: str, config=CONFIG) -> float | None:
    standard_deviation = next(
        (
            value
            for name, value in config.line_adjusted_stddev_by_sport
            if name.casefold() == sport.casefold()
        ),
        None,
    )
    if (
        standard_deviation is None
        or not isfinite(standard_deviation)
        or standard_deviation <= 0
    ):
        return None
    return standard_deviation


def estimate_line_adjusted_probability(
    sport: str,
    market: str,
    selection: str,
    target_point: float,
    references: list[dict[str, Any]],
    config=CONFIG,
) -> dict[str, Any] | None:
    if market not in {"spreads", "totals"} or not isfinite(target_point):
        return None

    standard_deviation = _standard_deviation_for_sport(sport, config)
    usable = []
    for reference in references:
        try:
            point = float(reference["point"])
            probability = float(reference["fair_probability"])
            overround = float(reference.get("overround", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        coordinate = _selection_coordinate(market, selection, point)
        if (
            coordinate is not None
            and isfinite(point)
            and isfinite(probability)
            and 0 < probability < 1
            and isfinite(overround)
        ):
            usable.append(
                {
                    **reference,
                    "point": point,
                    "coordinate": coordinate,
                    "fair_probability": probability,
                    "overround": overround,
                }
            )
    if not usable:
        return None

    nearest_distance = min(abs(item["point"] - target_point) for item in usable)
    if nearest_distance > config.max_line_diff + 1e-9:
        return None
    target_coordinate = _selection_coordinate(market, selection, target_point)
    if target_coordinate is None:
        return None

    exact = next(
        (item for item in usable if abs(item["point"] - target_point) <= 1e-6),
        None,
    )
    if exact is not None:
        probability = exact["fair_probability"]
        win, lose, push = _normal_outcome_probabilities(
            probability,
            standard_deviation,
            _is_whole_line(target_point),
        ) if _is_whole_line(target_point) and standard_deviation else (
            probability,
            1 - probability,
            0.0,
        )
        return {
            "method": "exact",
            "fair_probability": probability,
            "win_probability": win,
            "lose_probability": lose,
            "push_probability": push,
            "source_references": [exact],
        }

    ordered = sorted(usable, key=lambda item: item["coordinate"])
    bracket = None
    for left, right in zip(ordered, ordered[1:]):
        if (
            left["coordinate"] < target_coordinate < right["coordinate"]
            and abs(left["point"] - target_point) <= config.max_line_diff + 1e-9
            and abs(right["point"] - target_point) <= config.max_line_diff + 1e-9
        ):
            bracket = (left, right)
            break

    if bracket is not None:
        left, right = bracket
        fraction = (
            (target_coordinate - left["coordinate"])
            / (right["coordinate"] - left["coordinate"])
        )
        probability = left["fair_probability"] + fraction * (
            right["fair_probability"] - left["fair_probability"]
        )
        if _is_whole_line(target_point):
            if standard_deviation is None or standard_deviation <= 0:
                return None
            win, lose, push = _normal_outcome_probabilities(
                probability, standard_deviation, True
            )
        else:
            win, lose, push = probability, 1 - probability, 0.0
        return {
            "method": "interpolation",
            "fair_probability": probability,
            "win_probability": win,
            "lose_probability": lose,
            "push_probability": push,
            "source_references": [left, right],
        }

    if standard_deviation is None or standard_deviation <= 0:
        return None
    mainline = min(
        usable,
        key=lambda item: (
            abs(item["fair_probability"] - 0.5),
            item["overround"],
        ),
    )
    if abs(mainline["point"] - target_point) > config.max_line_diff + 1e-9:
        return None

    anchor_whole = _is_whole_line(mainline["point"])
    mean_at_anchor = _normal_mean_for_probability(
        mainline["fair_probability"], standard_deviation, anchor_whole
    )
    anchor_coordinate = _selection_coordinate(
        market, selection, mainline["point"]
    )
    target_whole = _is_whole_line(target_point)
    target_mean = mean_at_anchor + target_coordinate - anchor_coordinate
    probability = _normal_conditional_probability(
        target_mean, standard_deviation, target_whole
    )

    if sport.casefold() in {"nfl", "ncaaf"} and market == "spreads":
        direction = 1 if target_coordinate > anchor_coordinate else -1
        key_adjustment = sum(
            direction * boost
            for key_number, boost in config.football_key_number_probability_boosts
            for signed_key in (-key_number, key_number)
            if min(mainline["point"], target_point)
            < signed_key
            < max(mainline["point"], target_point)
        )
        probability = min(max(probability + key_adjustment, 1e-9), 1 - 1e-9)

    if target_whole:
        win, lose, push = _normal_outcome_probabilities(
            probability, standard_deviation, True
        )
    else:
        win, lose, push = probability, 1 - probability, 0.0
    return {
        "method": "distribution",
        "fair_probability": probability,
        "win_probability": win,
        "lose_probability": lose,
        "push_probability": push,
        "source_references": [mainline],
    }


def _valid_two_way_market(
    market: str, quotes_by_selection: dict[str, dict[str, Any]], expected: set[str]
) -> bool:
    if len(expected) != 2 or set(quotes_by_selection) != expected:
        return False
    if market == "spreads":
        points = [_numeric_point(quotes_by_selection[selection]) for selection in expected]
        return (
            all(point is not None and point != 0 for point in points)
            and abs(abs(points[0]) - abs(points[1])) <= 1e-6
            and points[0] * points[1] < 0
            and abs(points[0] + points[1]) <= 1e-6
        )
    if market == "totals":
        points = [_numeric_point(quotes_by_selection[selection]) for selection in expected]
        return (
            all(point is not None for point in points)
            and abs(points[0] - points[1]) <= 1e-6
        )
    return market == "h2h"


def _quote_is_stale(
    quote: dict[str, Any], now: datetime, max_age_seconds: int
) -> bool:
    timestamp = _quote_time(quote)
    return bool(
        timestamp is not None
        and (now - timestamp).total_seconds() > max_age_seconds
    )


def _american_from_decimal(decimal_odds: float) -> int:
    if decimal_odds >= 2:
        return int(round((decimal_odds - 1) * 100))
    return int(round(-100 / (decimal_odds - 1)))


def _is_pinnacle_quote(quote: dict[str, Any], config=CONFIG) -> bool:
    return (
        str(quote.get("bookmaker_key") or "").casefold()
        == config.pinnacle_bookmaker_key
        or str(quote.get("sportsbook") or "").casefold()
        == config.pinnacle_bookmaker_name.casefold()
    )


def _event_started(quote: dict[str, Any], now: datetime) -> tuple[bool, bool]:
    if quote.get("completed") is True or quote.get("is_live") is True:
        return True, False
    kickoff = _parse_utc(quote.get("kickoff"))
    if kickoff is None:
        return False, True
    return kickoff <= now, False


def _drop_reason_for_missing_reference(
    ref_key: tuple[Any, ...] | None,
    reference_status: dict[tuple[Any, ...], dict[str, Any]],
) -> str:
    if ref_key is None:
        return "line_mismatch"
    status = reference_status.get(ref_key)
    if status:
        return status["status"]

    sport, event_id, market, period, team_scope, _line = ref_key
    same_event_market = [
        key
        for key in reference_status
        if key[0] == sport and key[1] == event_id and key[2] == market
    ]
    same_scope_period = [
        key
        for key in same_event_market
        if key[3] == period and key[4] == team_scope
    ]
    if same_scope_period:
        return "line_mismatch"
    if any(key[3] != period for key in same_event_market):
        return "period_mismatch"
    if market == "totals" and any(
        key[3] == period and key[4] != team_scope for key in same_event_market
    ):
        return "team_scope_mismatch"
    return "no_pinnacle_reference"


def deduplicate_alt_line_ladders(
    candidate_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    best_by_selection: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in candidate_rows:
        market = _base_market(row)
        key = (
            row.get("sport", ""),
            row.get("event_id", ""),
            market,
            row.get("period", ""),
            str(row.get("team") or "").strip().casefold()
            if market == "totals"
            else "",
            row.get("selection", ""),
        )
        current = best_by_selection.get(key)
        if current is None or row["ev_pct"] > current["ev_pct"]:
            best_by_selection[key] = row
    opportunities = sorted(
        best_by_selection.values(), key=lambda row: row["ev_pct"], reverse=True
    )
    return opportunities, len(candidate_rows) - len(best_by_selection)


def scan_pinnacle_ev(
    quotes: list[dict[str, Any]],
    config=CONFIG,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)

    diagnostics = {
        "rows_fetched": len(quotes),
        "bettable_quotes": 0,
        "dropped": {
            "no_pinnacle_reference": 0,
            "one_sided_pinnacle_market": 0,
            "line_mismatch": 0,
            "period_mismatch": 0,
            "team_scope_mismatch": 0,
            "stale_quote": 0,
            "overround_too_high": 0,
            "started": 0,
            "missing_start_time": 0,
            "invalid_pinnacle_market": 0,
            "below_min_ev": 0,
            "alt_line_duplicate": 0,
        },
        "rows_kept": 0,
        "ev_pct": {"min": None, "median": None, "max": None},
    }

    pinnacle_quotes = [
        quote for quote in quotes if _is_pinnacle_quote(quote, config)
    ]
    bettable_quotes = [
        quote for quote in quotes if not _is_pinnacle_quote(quote, config)
    ]
    diagnostics["bettable_quotes"] = len(bettable_quotes)

    grouped_pinnacle: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    reference_status: dict[tuple[Any, ...], dict[str, Any]] = {}
    for quote in pinnacle_quotes:
        key = _reference_key(quote)
        if key is not None:
            grouped_pinnacle[key].append(quote)

    valid_references: dict[tuple[Any, ...], dict[str, Any]] = {}
    pinnacle_snapshots = []
    for key, market_quotes in grouped_pinnacle.items():
        market = key[2]
        expected_values = [
            set(quote.get("expected_outcomes") or [])
            for quote in market_quotes
            if quote.get("expected_outcomes")
        ]
        expected = expected_values[0] if expected_values else {
            quote.get("selection") for quote in market_quotes
        }
        by_selection = {}
        for quote in market_quotes:
            selection = quote.get("selection")
            if isinstance(selection, str):
                by_selection[selection] = quote
        if len(expected) != 2:
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if len(expected_values) > 1 and any(
            value != expected for value in expected_values[1:]
        ):
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if len(by_selection) > 2:
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if len(by_selection) < 2 or set(by_selection) != expected:
            reference_status[key] = {"status": "one_sided_pinnacle_market"}
            continue
        if not _valid_two_way_market(market, by_selection, expected):
            reference_status[key] = {"status": "line_mismatch"}
            continue

        selection_names = list(expected)
        quote_a = by_selection[selection_names[0]]
        quote_b = by_selection[selection_names[1]]
        try:
            decimal_a = float(quote_a["odds_decimal"])
            decimal_b = float(quote_b["odds_decimal"])
            overround = 1 / decimal_a + 1 / decimal_b
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if (
            not isfinite(decimal_a)
            or not isfinite(decimal_b)
            or decimal_a <= 1
            or decimal_b <= 1
            or overround < 1 - 1e-12
        ):
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        if overround > config.max_pinnacle_overround:
            reference_status[key] = {"status": "overround_too_high"}
            continue
        try:
            fair_a, fair_b = devig_two_way(
                decimal_a, decimal_b, method=config.devig_method
            )
        except ValueError:
            reference_status[key] = {"status": "invalid_pinnacle_market"}
            continue
        fair_by_selection = {
            selection_names[0]: fair_a,
            selection_names[1]: fair_b,
        }
        reference = {
            "quotes": by_selection,
            "fair_by_selection": fair_by_selection,
            "overround": overround,
            "selections": selection_names,
        }
        valid_references[key] = reference
        reference_status[key] = {"status": "valid", **reference}

        is_stale = any(
            _quote_is_stale(quote, now_utc, config.max_quote_age_seconds)
            for quote in (quote_a, quote_b)
        )
        started, missing_start = _event_started(quote_a, now_utc)
        if not is_stale and not started and not missing_start:
            for selection in selection_names:
                pinnacle_quote = by_selection[selection]
                pinnacle_snapshots.append(
                    {
                        "sport": pinnacle_quote.get("sport", ""),
                        "event_id": pinnacle_quote.get("event_id", ""),
                        "market_key": _base_market(pinnacle_quote),
                        "period": pinnacle_quote.get("period", ""),
                        "team": pinnacle_quote.get("team") or "",
                        "selection": selection,
                        "point": pinnacle_quote.get("point"),
                        "kickoff": pinnacle_quote.get("kickoff", ""),
                        "fair_probability": fair_by_selection[selection],
                        "pinnacle_american": pinnacle_quote.get("odds_american")
                        or _american_from_decimal(float(pinnacle_quote["odds_decimal"])),
                        "pinnacle_decimal": float(pinnacle_quote["odds_decimal"]),
                        "overround": overround,
                        "quote_time": _quote_time_text(pinnacle_quote),
                    }
                )

    references_by_scope: dict[
        tuple[Any, ...], list[tuple[tuple[Any, ...], dict[str, Any]]]
    ] = defaultdict(list)
    for reference_key, reference in valid_references.items():
        references_by_scope[reference_key[:5]].append((reference_key, reference))

    candidate_rows = []
    for quote in bettable_quotes:
        started, missing_start = _event_started(quote, now_utc)
        if started:
            diagnostics["dropped"]["started"] += 1
            continue
        if missing_start:
            diagnostics["dropped"]["missing_start_time"] += 1
            continue

        key = _reference_key(quote)
        reference = valid_references.get(key)
        line_pricing = None
        if (
            reference is None
            and config.enable_line_adjusted
            and key is not None
            and key not in reference_status
            and key[2] in {"spreads", "totals"}
        ):
            selection = quote.get("selection")
            candidate_expected = set(quote.get("expected_outcomes") or [])
            line_references = []
            if isinstance(selection, str):
                for _, candidate_reference in references_by_scope.get(
                    key[:5], []
                ):
                    if candidate_expected and candidate_expected != set(
                        candidate_reference["selections"]
                    ):
                        continue
                    side_quote = candidate_reference["quotes"].get(selection)
                    if side_quote is None:
                        continue
                    pair = list(candidate_reference["quotes"].values())
                    if any(
                        _quote_is_stale(
                            item, now_utc, config.max_quote_age_seconds
                        )
                        for item in pair
                    ):
                        continue
                    side_point = _numeric_point(side_quote)
                    if side_point is None:
                        continue
                    line_references.append(
                        {
                            "point": side_point,
                            "fair_probability": candidate_reference[
                                "fair_by_selection"
                            ][selection],
                            "overround": candidate_reference["overround"],
                            "reference": candidate_reference,
                        }
                    )

            candidate_point = _numeric_point(quote)
            if candidate_point is not None:
                line_pricing = estimate_line_adjusted_probability(
                    str(quote.get("sport") or ""),
                    key[2],
                    selection,
                    candidate_point,
                    line_references,
                    config,
                )
                if line_pricing is not None:
                    primary_line_reference = min(
                        line_pricing["source_references"],
                        key=lambda item: abs(item["point"] - candidate_point),
                    )
                    reference = primary_line_reference["reference"]

        if reference is None:
            reason = _drop_reason_for_missing_reference(key, reference_status)
            diagnostics["dropped"][reason] += 1
            continue

        ref_status = reference_status.get(key, {}) if line_pricing is None else {}
        if line_pricing is None and ref_status.get("status") != "valid":
            diagnostics["dropped"][
                ref_status.get("status", "no_pinnacle_reference")
            ] += 1
            continue
        if reference["overround"] > config.max_pinnacle_overround:
            diagnostics["dropped"]["overround_too_high"] += 1
            continue
        pinnacle_pair = list(reference["quotes"].values())
        if any(
            _quote_is_stale(item, now_utc, config.max_quote_age_seconds)
            for item in pinnacle_pair
        ) or _quote_is_stale(quote, now_utc, config.max_quote_age_seconds):
            diagnostics["dropped"]["stale_quote"] += 1
            continue

        selection = quote.get("selection")
        candidate_expected = set(quote.get("expected_outcomes") or [])
        if candidate_expected and candidate_expected != set(reference["selections"]):
            diagnostics["dropped"]["line_mismatch"] += 1
            continue
        fair_probability = (
            line_pricing["fair_probability"]
            if line_pricing is not None
            else reference["fair_by_selection"].get(selection)
        )
        pinnacle_side = reference["quotes"].get(selection)
        opposite_selection = next(
            (side for side in reference["selections"] if side != selection),
            None,
        )
        pinnacle_other = reference["quotes"].get(opposite_selection)
        if (
            fair_probability is None
            or pinnacle_side is None
            or pinnacle_other is None
        ):
            diagnostics["dropped"]["line_mismatch"] += 1
            continue
        if line_pricing is None and _base_market(quote) == "spreads":
            candidate_point = _numeric_point(quote)
            ref_point = _numeric_point(pinnacle_side)
            if (
                candidate_point is None
                or ref_point is None
                or abs(candidate_point - ref_point) > 1e-6
            ):
                diagnostics["dropped"]["line_mismatch"] += 1
                continue

        try:
            decimal_odds = float(quote["odds_decimal"])
            if line_pricing is None:
                win_probability = fair_probability
                lose_probability = 1 - fair_probability
                push_probability = 0.0
                market = _base_market(quote)
                exact_point = _numeric_point(quote)
                if (
                    market in {"spreads", "totals"}
                    and exact_point is not None
                    and _is_whole_line(exact_point)
                ):
                    standard_deviation = _standard_deviation_for_sport(
                        str(quote.get("sport") or ""), config
                    )
                    if standard_deviation is not None:
                        (
                            win_probability,
                            lose_probability,
                            push_probability,
                        ) = _normal_outcome_probabilities(
                            fair_probability, standard_deviation, True
                        )
                ev_pct = (
                    expected_value_with_push_pct(
                        decimal_odds, win_probability, lose_probability
                    )
                    if push_probability > 0
                    else expected_value_pct(decimal_odds, fair_probability)
                )
            else:
                win_probability = line_pricing["win_probability"]
                lose_probability = line_pricing["lose_probability"]
                push_probability = line_pricing["push_probability"]
                ev_pct = expected_value_with_push_pct(
                    decimal_odds, win_probability, lose_probability
                )
        except (KeyError, TypeError, ValueError):
            diagnostics["dropped"]["invalid_pinnacle_market"] += 1
            continue
        if ev_pct < config.min_ev_pct:
            diagnostics["dropped"]["below_min_ev"] += 1
            continue

        source_references = (
            line_pricing["source_references"]
            if line_pricing is not None
            else [
                {
                    "point": _numeric_point(pinnacle_side),
                    "reference": reference,
                }
            ]
        )
        source_lines = sorted(
            {item["point"] for item in source_references if item["point"] is not None}
        )
        line_adjustment_method = (
            line_pricing["method"] if line_pricing is not None else ""
        )
        flags = []
        if line_pricing is not None:
            flags.append("line-adjusted")
        if ev_pct > config.verify_ev_pct:
            flags.append("verify")
        row = {
            **quote,
            "market_key": _base_market(quote),
            "fair_probability": fair_probability,
            "win_probability": win_probability,
            "lose_probability": lose_probability,
            "push_probability": push_probability,
            "ev_pct": ev_pct,
            "ev_per_100": ev_pct,
            "pinnacle_american_this_side": pinnacle_side.get("odds_american")
            or _american_from_decimal(float(pinnacle_side["odds_decimal"])),
            "pinnacle_american_other_side": pinnacle_other.get("odds_american")
            or _american_from_decimal(float(pinnacle_other["odds_decimal"])),
            "pinnacle_selection_other_side": opposite_selection,
            "pinnacle_overround": reference["overround"],
            "pinnacle_quote_time": _quote_time_text(pinnacle_side),
            "book_quote_time": _quote_time_text(quote),
            "line": quote.get("point"),
            "pinnacle_line": _numeric_point(pinnacle_side),
            "pinnacle_lines_used": ", ".join(
                f"{point:g}" for point in source_lines
            ),
            "pricing_method": (
                "line-adjusted" if line_pricing is not None else "exact"
            ),
            "line_adjustment_method": line_adjustment_method,
            "flags": ", ".join(flags),
            "reference": "Pinnacle",
        }
        if line_pricing is not None:
            row["kelly_fraction"] = (
                config.effective_line_adjusted_kelly_fraction
            )
        candidate_rows.append(row)

    opportunities, duplicate_count = deduplicate_alt_line_ladders(candidate_rows)
    diagnostics["dropped"]["alt_line_duplicate"] = duplicate_count
    diagnostics["rows_kept"] = len(opportunities)
    if opportunities:
        ev_values = [row["ev_pct"] for row in opportunities]
        diagnostics["ev_pct"] = {
            "min": min(ev_values),
            "median": median(ev_values),
            "max": max(ev_values),
        }
    return {
        "opportunities": opportunities,
        "eligible_rows": sorted(
            candidate_rows, key=lambda row: row["ev_pct"], reverse=True
        ),
        "pinnacle_snapshots": pinnacle_snapshots,
        "diagnostics": diagnostics,
    }


def _compatible_market_lines(
    market_key: str,
    outcome_quotes: dict[str, dict[str, Any]],
    outcomes: set[str],
) -> bool:
    market_type, separator, line_identity = market_key.partition(":")
    if not separator:
        return market_type not in {"spreads", "totals"}
    try:
        expected_line = float(line_identity.split(":", 1)[0])
    except ValueError:
        return False
    if not isfinite(expected_line):
        return False

    if market_key.startswith("spreads"):
        if len(outcomes) != 2:
            return False
        try:
            points = [float(outcome_quotes[outcome]["point"]) for outcome in outcomes]
        except (KeyError, TypeError, ValueError):
            return False
        return (
            all(isfinite(point) and point != 0 for point in points)
            and all(abs(abs(point) - expected_line) <= 1e-6 for point in points)
            and points[0] * points[1] < 0
            and abs(points[0] + points[1]) <= 1e-6
        )
    if market_key.startswith("totals"):
        try:
            points = [float(outcome_quotes[outcome]["point"]) for outcome in outcomes]
        except (KeyError, TypeError, ValueError):
            return False
        return (
            bool(points)
            and all(isfinite(point) for point in points)
            and all(abs(point - expected_line) <= 1e-6 for point in points)
            and max(points) - min(points) <= 1e-6
        )
    return True


def _allocate_rounded_stakes(
    odds: list[float], total_stake: int, rounding_step: int, target_stakes: list[float]
) -> list[int] | None:
    budget_units, remainder = divmod(total_stake, rounding_step)
    remainder_options = [None] if remainder == 0 else range(len(odds))
    allocations = []

    for remainder_index in remainder_options:
        def required_units(target_cents: int) -> list[int]:
            target_dollars = target_cents / 100
            units = []
            for index, price in enumerate(odds):
                extra_stake = remainder if index == remainder_index else 0
                minimum_units = 0 if extra_stake else 1
                units_needed = ceil(
                    (target_dollars - extra_stake * price) / (rounding_step * price)
                    - 1e-10
                )
                units.append(max(minimum_units, units_needed))
            return units

        low = 0
        high = int(total_stake * min(odds) * 100)
        while low < high:
            target = (low + high + 1) // 2
            if sum(required_units(target)) <= budget_units:
                low = target
            else:
                high = target - 1

        units = required_units(low)
        if sum(units) > budget_units:
            continue
        stakes = [
            count * rounding_step
            + (remainder if index == remainder_index else 0)
            for index, count in enumerate(units)
        ]
        unused_units = budget_units - sum(units)
        while unused_units:
            lowest_payout = min(
                range(len(odds)), key=lambda index: stakes[index] * odds[index]
            )
            stakes[lowest_payout] += rounding_step
            unused_units -= 1
        payouts = [stake * price for stake, price in zip(stakes, odds)]
        distance_from_target = sum(
            abs(stake - target) for stake, target in zip(stakes, target_stakes)
        )
        allocations.append((min(payouts), -distance_from_target, stakes))

    if not allocations:
        return None
    return max(allocations, key=lambda allocation: (allocation[0], allocation[1]))[2]


def find_arbitrage_opportunities(
    quotes: list[dict[str, Any]],
    total_stake: float,
    rounding_step: int = 1,
    config=CONFIG,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if not isfinite(total_stake) or total_stake <= 0:
        raise ValueError("Total stake must be greater than zero.")
    if not float(total_stake).is_integer():
        raise ValueError("Total stake must be a whole-dollar amount.")
    if (
        isinstance(rounding_step, bool)
        or not isinstance(rounding_step, int)
        or rounding_step < 1
    ):
        raise ValueError("Rounding step must be a positive whole-dollar amount.")
    total_stake = int(total_stake)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)

    markets: dict[
        tuple[str, str, str, str], dict[str, list[dict[str, Any]]]
    ] = defaultdict(lambda: defaultdict(list))
    for quote in quotes:
        if _quote_time(quote) is None or _quote_is_stale(
            quote, now_utc, config.max_quote_age_seconds
        ):
            continue
        odds = float(quote["odds_decimal"])
        if not isfinite(odds) or odds <= 1:
            continue
        market_key = (
            quote["sport"],
            quote.get("event_id", quote["matchup"]),
            quote.get("market_key", quote["market"]),
            quote.get("period", ""),
        )
        markets[market_key][quote["selection"]].append(quote)

    opportunities = []
    for (sport, event_id, market_key, period), selection_quotes in markets.items():
        if len(selection_quotes) != 2:
            continue
        expected_outcomes = next(
            (
                set(quote["expected_outcomes"])
                for quotes_for_selection in selection_quotes.values()
                for quote in quotes_for_selection
                if quote.get("expected_outcomes")
            ),
            None,
        )
        if expected_outcomes is not None and (
            len(expected_outcomes) != 2 or set(selection_quotes) != expected_outcomes
        ):
            continue

        selection_names = list(expected_outcomes or selection_quotes)
        first_quotes = selection_quotes[selection_names[0]]
        second_quotes = selection_quotes[selection_names[1]]
        compatible_pairs = []
        for first_quote in first_quotes:
            for second_quote in second_quotes:
                if first_quote["sportsbook"] == second_quote["sportsbook"]:
                    continue
                if str(market_key).startswith("spreads"):
                    if not _compatible_market_lines(
                        market_key,
                        {
                            selection_names[0]: first_quote,
                            selection_names[1]: second_quote,
                        },
                        set(selection_names),
                    ):
                        continue
                elif str(market_key).startswith(
                    "totals"
                ) and not _compatible_market_lines(
                    market_key,
                    {
                        selection_names[0]: first_quote,
                        selection_names[1]: second_quote,
                    },
                    set(selection_names),
                ):
                    continue
                odds_sum = (
                    1 / float(first_quote["odds_decimal"])
                    + 1 / float(second_quote["odds_decimal"])
                )
                compatible_pairs.append((odds_sum, first_quote, second_quote))
        if not compatible_pairs:
            continue
        implied_probability_sum, first_quote, second_quote = min(
            compatible_pairs, key=lambda pair: pair[0]
        )
        if implied_probability_sum >= 1 or total_stake < 2:
            continue

        selection_rows = [
            (selection_names[0], first_quote),
            (selection_names[1], second_quote),
        ]
        odds = [float(quote["odds_decimal"]) for _, quote in selection_rows]
        raw_stakes = [
            total_stake * (1 / price) / implied_probability_sum
            for price in odds
        ]
        stakes = _allocate_rounded_stakes(
            odds, total_stake, rounding_step, raw_stakes
        )
        if stakes is None:
            continue

        payouts = [stake * price for stake, price in zip(stakes, odds)]
        guaranteed_payout = min(payouts)
        profit = round(guaranteed_payout - total_stake, 2)
        if profit <= 0:
            continue

        legs = []
        for stake, (selection, quote) in zip(stakes, selection_rows):
            odds = float(quote["odds_decimal"])
            legs.append(
                {
                    "selection": selection,
                    "market": quote["market"],
                    "sportsbook": quote["sportsbook"],
                    "odds_decimal": odds,
                    "odds_american": quote.get("odds_american"),
                    "liquidity": quote.get("liquidity"),
                    "liquidity_updated_at": quote.get("liquidity_updated_at"),
                    "stake": stake,
                    "payout": round(stake * odds, 2),
                }
            )

        opportunities.append(
            {
                "sport": sport,
                "matchup": first_quote["matchup"],
                "kickoff": first_quote.get("kickoff", ""),
                "market": first_quote["market"],
                "event_id": event_id,
                "market_key": market_key,
                "period": period,
                "implied_probability_sum": implied_probability_sum,
                "theoretical_roi_pct": (1 / implied_probability_sum - 1) * 100,
                "theoretical_profit": total_stake
                * (1 / implied_probability_sum - 1),
                "theoretical_locked_payout": total_stake / implied_probability_sum,
                "roi_pct": profit / total_stake * 100,
                "total_stake": total_stake,
                "locked_payout": round(guaranteed_payout, 2),
                "profit": profit,
                "legs": legs,
            }
        )
    return sorted(opportunities, key=lambda item: item["roi_pct"], reverse=True)
