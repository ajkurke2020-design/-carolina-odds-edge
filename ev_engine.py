from math import floor, isfinite
from typing import Any

from scanner_config import CONFIG


def decimal_to_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int(round((decimal - 1.0) * 100))
    return int(round(-100.0 / (decimal - 1.0)))


def remove_vig(odds_list: list) -> list:
    implied_probs = [1.0 / odds for odds in odds_list]
    total_ip = sum(implied_probs)
    return [ip / total_ip for ip in implied_probs]


def calculate_kelly(
    bankroll: float,
    fair_prob: float,
    decimal_odds: float,
    fraction: float = CONFIG.kelly_fraction,
) -> float:
    if (
        not isfinite(bankroll)
        or bankroll < 0
        or not isfinite(fair_prob)
        or not 0 <= fair_prob <= 1
        or not isfinite(decimal_odds)
        or decimal_odds <= 1
        or not isfinite(fraction)
        or not 0 <= fraction <= 1
    ):
        raise ValueError("Bankroll, probability, odds, and Kelly fraction are invalid.")
    full_kelly = (fair_prob * decimal_odds - 1) / (decimal_odds - 1)
    recommended = bankroll * max(0.0, full_kelly) * fraction
    return round(recommended, 2)


def calculate_capped_kelly_stakes(
    opportunities: list[dict[str, Any]],
    bankroll: float,
    fraction: float,
    per_bet_cap_fraction: float = CONFIG.max_bet_pct,
    per_event_cap_fraction: float = CONFIG.max_event_pct,
) -> list[float]:
    if not isfinite(bankroll) or bankroll < 0:
        raise ValueError("Bankroll must be a nonnegative finite amount.")
    if not isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError("Kelly fraction must be between 0 and 1.")
    if (
        not isfinite(per_bet_cap_fraction)
        or not 0 <= per_bet_cap_fraction <= 1
        or not isfinite(per_event_cap_fraction)
        or not 0 <= per_event_cap_fraction <= 1
    ):
        raise ValueError("Stake caps must be between 0 and 1.")

    raw_stakes = []
    events: dict[tuple[str, str], list[int]] = {}
    for index, quote in enumerate(opportunities):
        quote_fraction = quote.get("kelly_fraction", fraction)
        raw_stake = min(
            calculate_kelly(
                bankroll,
                quote["fair_probability"],
                quote["odds_decimal"],
                quote_fraction,
            ),
            bankroll * per_bet_cap_fraction,
        )
        raw_stakes.append(raw_stake)
        event_key = (
            quote.get("sport", ""),
            quote.get("event_id", quote.get("matchup", "")),
        )
        events.setdefault(event_key, []).append(index)

    recommended = [0.0] * len(opportunities)
    for indices in events.values():
        event_total = sum(raw_stakes[index] for index in indices)
        event_cap = bankroll * per_event_cap_fraction
        scale = min(1.0, event_cap / event_total) if event_total else 0.0
        for index in indices:
            capped_stake = raw_stakes[index] * scale
            recommended[index] = float(
                floor((capped_stake + 1e-9) * 100) / 100
            )
    return recommended


def round_stake(
    stake: float, rounding_step: int = CONFIG.arbitrage_rounding_step
) -> float:
    return float(round(stake / rounding_step) * rounding_step)
