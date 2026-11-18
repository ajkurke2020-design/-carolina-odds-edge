#!/usr/bin/env python3
"""
Standalone background worker for automated +EV bet and Arbitrage notifications.

Scans active sports via PropLine API, detects +EV opportunities against Pinnacle
and risk-free arbitrage opportunities across approved books, caches odds data,
tracks alert history to avoid duplicate notifications, and sends alerts to Telegram.
"""

from __future__ import annotations

import html
import json
import os
import socket
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.util import connection as urllib3_connection

from ev_engine import calculate_capped_kelly_stakes, decimal_to_american
from propline_api import SPORT_CONFIG, run_api
from scanner_config import CONFIG
from scanner_logic import find_arbitrage_opportunities, scan_pinnacle_ev

# Active sports to scan (MLB commented out for the offseason)
ACTIVE_SPORTS = [
    "Tennis",
    "NCAAB",
    "NBA",
    "NHL",
    "NFL",
    "NCAAF",  # College Football (CFB)
    # "MLB",  # Offseason
]

ALERT_HISTORY_FILE = "alert_history.json"
CACHED_ODDS_FILE = "cached_odds.json"
DEFAULT_MIN_EV_THRESHOLD = 3.0
DEFAULT_MIN_EV_INCREASE_FOR_RE_ALERT = 1.5
DEFAULT_MIN_ROI_INCREASE_FOR_ARB = 0.5
MAX_HISTORY_AGE_SECONDS = 48 * 3600  # 48 hours
ARBITRAGE_TOTAL_STAKE = 1000.0  # $1,000 stake for arbitrage opportunities


# --- IPv4 Adapter for Telegram API (prevents IPv6 connection resets on Windows/ISPs) ---
class _IPv4HTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        address = socket.getaddrinfo(
            self._dns_host, self.port, socket.AF_INET, socket.SOCK_STREAM
        )[0][4][0]
        return urllib3_connection.create_connection(
            (address, self.port),
            self.timeout,
            source_address=self.source_address,
            socket_options=self.socket_options,
        )


class _IPv4HTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _IPv4HTTPSConnection


class _TelegramIPv4Adapter(HTTPAdapter):
    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any) -> None:
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
        self.poolmanager.pool_classes_by_scheme["https"] = _IPv4HTTPSConnectionPool


def _new_telegram_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://api.telegram.org/", _TelegramIPv4Adapter())
    return session


def get_api_key() -> str:
    """Retrieve PropLine API key from environment variables."""
    for key_name in ("PROPLINE_API_KEY", "PROP_LINE_API_KEY", "API_KEY"):
        val = os.getenv(key_name, "").strip()
        if val:
            return val
    return ""


def get_telegram_credentials() -> tuple[str, str]:
    """Retrieve Telegram bot token and chat ID from environment variables."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return bot_token, chat_id


def is_game_started(commence_time_str: str | None, now_utc: datetime) -> bool:
    """Check if the event's commence time is at or before the current UTC time."""
    if not commence_time_str or not isinstance(commence_time_str, str):
        return False
    try:
        dt = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= now_utc
    except (ValueError, TypeError):
        return False


def build_alert_key(opp: dict[str, Any]) -> str:
    """
    Generate unique tracking key for a +EV opportunity:
    {game_id}_{market}_{selection}_{line}_{sportsbook}
    """
    game_id = str(opp.get("event_id") or opp.get("game_id") or "")
    market = str(opp.get("market") or opp.get("market_key") or "")
    selection = str(opp.get("selection") or "")
    line_val = opp.get("line") if opp.get("line") is not None else opp.get("point")
    line_str = str(line_val) if line_val is not None else "None"
    sportsbook = str(opp.get("sportsbook") or "")
    return f"{game_id}_{market}_{selection}_{line_str}_{sportsbook}"


def build_arbitrage_alert_key(arb: dict[str, Any]) -> str:
    """
    Generate unique tracking key for an arbitrage opportunity:
    arb_{game_id}_{market}_{sorted_legs}
    """
    game_id = str(arb.get("event_id") or "")
    market = str(arb.get("market") or arb.get("market_key") or "")
    legs_repr = "-".join(
        sorted(
            f"{leg.get('selection')}_{leg.get('sportsbook')}"
            for leg in arb.get("legs", [])
        )
    )
    return f"arb_{game_id}_{market}_{legs_repr}"


def load_alert_history(path: str = ALERT_HISTORY_FILE) -> dict[str, dict[str, Any]]:
    """Load alert history dictionary from disk."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as error:
        print(f"Warning: Could not read {path}: {error}. Starting with empty history.")
        return {}


def save_alert_history(
    history: dict[str, dict[str, Any]], path: str = ALERT_HISTORY_FILE
) -> None:
    """Persist alert history dictionary to disk."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as error:
        print(f"Error: Failed to save {path}: {error}", file=sys.stderr)


def clean_expired_history(
    history: dict[str, dict[str, Any]],
    now_ts: float,
    max_age_seconds: float = MAX_HISTORY_AGE_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Remove alert history entries older than 48 hours."""
    cleaned = {}
    for key, entry in history.items():
        if not isinstance(entry, dict):
            continue
        timestamp = entry.get("timestamp")
        if isinstance(timestamp, (int, float)) and (now_ts - timestamp) <= max_age_seconds:
            cleaned[key] = entry
    return cleaned


def should_alert(
    key: str,
    current_ev: float,
    history: dict[str, dict[str, Any]],
    min_increase: float = DEFAULT_MIN_EV_INCREASE_FOR_RE_ALERT,
) -> bool:
    """
    Determine whether to send a +EV alert:
    True if the key is new, OR if EV% on that exact same line increased by >= +1.5%.
    """
    if key not in history:
        return True
    prev_entry = history[key]
    prev_ev = float(prev_entry.get("ev", 0.0))
    return (current_ev - prev_ev) >= min_increase


def should_alert_arbitrage(
    key: str,
    current_roi: float,
    history: dict[str, dict[str, Any]],
    min_increase: float = DEFAULT_MIN_ROI_INCREASE_FOR_ARB,
) -> bool:
    """
    Determine whether to send an Arbitrage alert:
    True if the key is new, OR if ROI% on that opportunity increased by >= +0.5%.
    """
    if key not in history:
        return True
    prev_entry = history[key]
    prev_roi = float(prev_entry.get("roi", 0.0))
    return (current_roi - prev_roi) >= min_increase


def format_kelly_units(stake: float, bankroll: float = CONFIG.bankroll) -> str:
    """
    Format recommended Kelly stake into standard sports betting units (0.5u, 1u, etc.).
    1 unit is defined as 1% of the bankroll ($10 for a default $1,000 bankroll).
    Rounds to the nearest 0.5 unit.
    """
    unit_size = bankroll * 0.01
    if stake <= 0 or unit_size <= 0:
        return "0u"
    raw_units = stake / unit_size
    rounded = round(raw_units * 2) / 2
    if rounded == 0 and stake > 0:
        rounded = 0.5
    if rounded.is_integer():
        return f"{int(rounded)}u"
    return f"{rounded:g}u"


def format_telegram_alert(opp: dict[str, Any]) -> str:
    """
    Format +EV opportunity cleanly with HTML showing:
    Sport, Matchup, Bet/Line, Odds, Sportsbook, Fair Odds, EV%, and Recommended Kelly Units.
    """
    sport = html.escape(str(opp.get("sport") or "Unknown"))
    matchup = html.escape(str(opp.get("matchup") or "Unknown Matchup"))
    selection = html.escape(str(opp.get("selection") or ""))
    market = html.escape(str(opp.get("market") or ""))
    line = opp.get("line") if opp.get("line") is not None else opp.get("point")

    # Format Bet / Line
    if line is not None:
        try:
            line_num = float(line)
            line_str = f"{line_num:+g}" if "spread" in market.lower() else f"{line_num:g}"
        except (ValueError, TypeError):
            line_str = str(line)
        bet_line = f"{selection} {line_str}"
        if market and market.lower() not in bet_line.lower():
            bet_line += f" ({market})"
    else:
        bet_line = f"{selection} ({market})" if market else selection

    # Odds
    odds_am = opp.get("odds_american")
    odds_dec = opp.get("odds_decimal")
    if odds_am is not None:
        try:
            am_int = int(odds_am)
            odds_str = f"{am_int:+d}"
        except (ValueError, TypeError):
            odds_str = str(odds_am)
    elif odds_dec is not None:
        try:
            odds_str = f"{decimal_to_american(float(odds_dec)):+d}"
        except Exception:
            odds_str = "N/A"
    else:
        odds_str = "N/A"

    if odds_dec is not None:
        try:
            odds_str += f" ({float(odds_dec):.2f})"
        except (ValueError, TypeError):
            pass

    sportsbook = html.escape(str(opp.get("sportsbook") or "Unknown"))

    # Fair odds
    fair_prob = opp.get("fair_probability")
    if fair_prob is not None:
        try:
            fp = float(fair_prob)
            if 0 < fp < 1:
                fair_dec = 1.0 / fp
                fair_am = decimal_to_american(fair_dec)
                fair_str = f"{fair_am:+d} ({fair_dec:.2f}) [Fair Prob: {fp * 100:.1f}%]"
            else:
                fair_str = "N/A"
        except Exception:
            fair_str = "N/A"
    else:
        fair_str = "N/A"

    ev_pct = float(opp.get("ev_pct", 0.0))

    # Recommended Kelly Stake in units and dollars
    stake = float(opp.get("kelly_stake", 0.0))
    units_str = format_kelly_units(stake)
    stake_text = f"{units_str} (${stake:.2f})" if stake > 0 else "0u ($0.00)"

    return (
        f"🚨 <b>+EV Bet Opportunity</b> 🚨\n\n"
        f"🏆 <b>Sport:</b> {sport}\n"
        f"⚔️ <b>Matchup:</b> {matchup}\n"
        f"🎯 <b>Bet / Line:</b> {bet_line}\n"
        f"💰 <b>Odds:</b> {odds_str}\n"
        f"🏢 <b>Sportsbook:</b> {sportsbook}\n"
        f"⚖️ <b>Fair Odds:</b> {fair_str}\n"
        f"📈 <b>EV%:</b> +{ev_pct:.2f}%\n"
        f"💵 <b>Recommended Stake:</b> {stake_text}"
    )


def format_telegram_arbitrage_alert(arb: dict[str, Any]) -> str:
    """
    Format Arbitrage opportunity cleanly with HTML showing:
    Sport, Matchup, Market, Total Stake ($1,000 rounded to $1), Guaranteed Profit, ROI%, and Leg Stakes.
    """
    sport = html.escape(str(arb.get("sport") or "Unknown"))
    matchup = html.escape(str(arb.get("matchup") or "Unknown Matchup"))
    market = html.escape(str(arb.get("market") or ""))
    total_stake = float(arb.get("total_stake", ARBITRAGE_TOTAL_STAKE))
    profit = float(arb.get("profit", 0.0))
    roi_pct = float(arb.get("roi_pct", 0.0))
    locked_payout = float(arb.get("locked_payout", total_stake + profit))

    legs_html = []
    for i, leg in enumerate(arb.get("legs", []), start=1):
        sel = html.escape(str(leg.get("selection") or ""))
        book = html.escape(str(leg.get("sportsbook") or ""))
        stake = float(leg.get("stake", 0.0))
        payout = float(leg.get("payout", 0.0))
        odds_am = leg.get("odds_american")
        odds_dec = leg.get("odds_decimal")
        if odds_am is not None:
            try:
                am_int = int(odds_am)
                odds_str = f"{am_int:+d}"
            except (ValueError, TypeError):
                odds_str = str(odds_am)
        elif odds_dec is not None:
            odds_str = f"{decimal_to_american(float(odds_dec)):+d}"
        else:
            odds_str = "N/A"
        if odds_dec is not None:
            odds_str += f" ({float(odds_dec):.2f})"

        legs_html.append(
            f"<b>Leg {i}:</b>\n"
            f"• Bet: <b>{sel}</b> at <b>{book}</b>\n"
            f"• Odds: {odds_str}\n"
            f"• Stake: <b>${stake:,.0f}</b> (Payout: ${payout:,.2f})"
        )

    legs_text = "\n\n".join(legs_html)

    return (
        f"⚡ <b>Arbitrage Opportunity (Guaranteed Profit)</b> ⚡\n\n"
        f"🏆 <b>Sport:</b> {sport}\n"
        f"⚔️ <b>Matchup:</b> {matchup}\n"
        f"🎯 <b>Market:</b> {market}\n"
        f"💰 <b>Total Stake:</b> ${total_stake:,.0f} (nearest $1)\n"
        f"💵 <b>Guaranteed Profit:</b> +${profit:.2f} (ROI: +{roi_pct:.2f}%)\n"
        f"🔒 <b>Locked Payout:</b> ${locked_payout:,.2f}\n\n"
        f"{legs_text}"
    )


def send_telegram_message(
    bot_token: str, chat_id: str, message: str, timeout: int = 15
) -> bool:
    """Send message via Telegram Bot API sendMessage using IPv4 session."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    session = _new_telegram_session()
    try:
        response = session.post(url, json=payload, timeout=timeout)
        if response.status_code == 200:
            return True
        print(
            f"Telegram API error (HTTP {response.status_code}): {response.text}",
            file=sys.stderr,
        )
        return False
    except requests.RequestException as error:
        print(f"Telegram request failed: {error}", file=sys.stderr)
        return False
    finally:
        session.close()


def save_cached_odds(
    quotes: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    arbitrage: list[dict[str, Any]],
    sports: list[str],
    now_utc: datetime,
    path: str = CACHED_ODDS_FILE,
) -> None:
    """Cache fetched odds data, +EV opportunities, and Arbitrage to JSON."""
    data = {
        "cached_at": now_utc.isoformat(),
        "sports": sports,
        "quotes_count": len(quotes),
        "opportunities_count": len(opportunities),
        "arbitrage_count": len(arbitrage),
        "quotes": quotes,
        "opportunities": opportunities,
        "arbitrage": arbitrage,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as error:
        print(f"Error: Failed to save cached odds to {path}: {error}", file=sys.stderr)


def load_cached_odds(path: str = CACHED_ODDS_FILE) -> dict[str, Any] | None:
    """Read cached odds data from JSON if available."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception as error:
        print(f"Warning: Failed to load {path}: {error}")
        return None


def run_worker(
    sports: list[str] | None = None,
    min_ev: float = DEFAULT_MIN_EV_THRESHOLD,
    api_key: str | None = None,
    history_file: str = ALERT_HISTORY_FILE,
    cache_file: str = CACHED_ODDS_FILE,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute one scan-and-alert cycle for +EV and Arbitrage."""
    now_utc = datetime.now(timezone.utc)
    now_ts = time.time()
    scan_sports = sports or ACTIVE_SPORTS
    key = api_key if api_key is not None else get_api_key()

    print(f"[{now_utc.isoformat()}] Starting EV & Arbitrage alert worker...")
    print(f"Active sports: {', '.join(scan_sports)}")
    print(f"Minimum EV threshold: {min_ev:.1f}%")

    if not key:
        print(
            "ERROR: PropLine API key is missing. Set PROPLINE_API_KEY environment variable.",
            file=sys.stderr,
        )
        return {
            "success": False,
            "error": "Missing PropLine API key",
            "ev_alerts_sent": 0,
            "arb_alerts_sent": 0,
        }

    bot_token, chat_id = get_telegram_credentials()
    telegram_configured = bool(bot_token and chat_id)
    if not telegram_configured:
        print(
            "WARNING: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured. "
            "Alerts will be logged to console but not sent to Telegram."
        )

    # 1. Fetch upcoming quotes
    print("Fetching odds from PropLine API...")
    fetch_result = run_api(scan_sports, mode="upcoming", api_key=key)
    quotes = fetch_result.get("quotes", [])
    errors = fetch_result.get("errors", [])
    warnings = fetch_result.get("warnings", [])

    if errors:
        for err in errors:
            print(f"API Error: {err}", file=sys.stderr)
    if warnings:
        for warn in warnings:
            print(f"API Warning: {warn}")

    print(f"Fetched {len(quotes)} quotes across {len(scan_sports)} sports.")

    # 2. Run EV calculation against Pinnacle
    scanner_cfg = replace(CONFIG, min_ev_pct=min_ev)
    scan_res = scan_pinnacle_ev(quotes, config=scanner_cfg, now=now_utc)
    raw_opportunities = scan_res.get("opportunities", [])

    # Filter out games that have already started and EV < threshold
    valid_opportunities = []
    for opp in raw_opportunities:
        ev = float(opp.get("ev_pct", 0.0))
        if ev < min_ev:
            continue
        commence_time = opp.get("kickoff") or opp.get("commence_time")
        if is_game_started(commence_time, now_utc):
            continue
        valid_opportunities.append(opp)

    # Calculate Kelly stakes and units
    if valid_opportunities:
        scan_stakes = calculate_capped_kelly_stakes(
            valid_opportunities,
            bankroll=CONFIG.bankroll,
            fraction=CONFIG.kelly_fraction,
        )
        for opp, stake in zip(valid_opportunities, scan_stakes):
            opp["kelly_stake"] = stake

    print(f"Found {len(valid_opportunities)} eligible +EV opportunities (>= {min_ev:.1f}% EV and upcoming).")

    # 3. Run Arbitrage calculations ($1,000 total stake, nearest $1 rounding)
    raw_arbs = find_arbitrage_opportunities(
        quotes,
        total_stake=ARBITRAGE_TOTAL_STAKE,
        rounding_step=1,
        config=scanner_cfg,
        now=now_utc,
    )
    valid_arbs = []
    for arb in raw_arbs:
        commence_time = arb.get("kickoff") or arb.get("commence_time")
        if is_game_started(commence_time, now_utc):
            continue
        if float(arb.get("profit", 0.0)) > 0:
            valid_arbs.append(arb)

    print(f"Found {len(valid_arbs)} eligible Arbitrage opportunities (stake=${ARBITRAGE_TOTAL_STAKE:,.0f}).")

    # 4. Save cached odds
    save_cached_odds(quotes, valid_opportunities, valid_arbs, scan_sports, now_utc, cache_file)
    print(f"Saved {len(quotes)} quotes, {len(valid_opportunities)} EV opps, and {len(valid_arbs)} arbs to {cache_file}.")

    # 5. Load and clean alert history
    history = load_alert_history(history_file)
    initial_history_count = len(history)
    history = clean_expired_history(history, now_ts, MAX_HISTORY_AGE_SECONDS)
    pruned = initial_history_count - len(history)
    if pruned > 0:
        print(f"Pruned {pruned} alert history entries older than 48 hours.")

    # 6. Evaluate and dispatch +EV alerts
    ev_alerts_sent = 0
    for opp in valid_opportunities:
        alert_key = build_alert_key(opp)
        ev = float(opp.get("ev_pct", 0.0))

        if not should_alert(alert_key, ev, history):
            continue

        message = format_telegram_alert(opp)

        if telegram_configured and not dry_run:
            print(f"Sending Telegram +EV alert for {alert_key} (+{ev:.2f}% EV)...")
            success = send_telegram_message(bot_token, chat_id, message)
            if success:
                ev_alerts_sent += 1
                history[alert_key] = {"ev": ev, "timestamp": now_ts}
                time.sleep(0.15)
        else:
            print(f"[CONSOLE +EV ALERT] {alert_key} (+{ev:.2f}% EV):\n{message}\n")
            ev_alerts_sent += 1
            history[alert_key] = {"ev": ev, "timestamp": now_ts}

    # 7. Evaluate and dispatch Arbitrage alerts
    arb_alerts_sent = 0
    for arb in valid_arbs:
        arb_key = build_arbitrage_alert_key(arb)
        roi = float(arb.get("roi_pct", 0.0))

        if not should_alert_arbitrage(arb_key, roi, history):
            continue

        message = format_telegram_arbitrage_alert(arb)

        if telegram_configured and not dry_run:
            print(f"Sending Telegram Arbitrage alert for {arb_key} (+{roi:.2f}% ROI)...")
            success = send_telegram_message(bot_token, chat_id, message)
            if success:
                arb_alerts_sent += 1
                history[arb_key] = {"roi": roi, "timestamp": now_ts}
                time.sleep(0.15)
        else:
            print(f"[CONSOLE ARB ALERT] {arb_key} (+{roi:.2f}% ROI):\n{message}\n")
            arb_alerts_sent += 1
            history[arb_key] = {"roi": roi, "timestamp": now_ts}

    # 8. Persist updated history
    save_alert_history(history, history_file)
    print(
        f"Alert cycle complete. +EV alerts: {ev_alerts_sent}, Arb alerts: {arb_alerts_sent}. "
        f"Total active tracked items: {len(history)}."
    )

    return {
        "success": True,
        "quotes_fetched": len(quotes),
        "eligible_opportunities": len(valid_opportunities),
        "eligible_arbitrages": len(valid_arbs),
        "ev_alerts_sent": ev_alerts_sent,
        "arb_alerts_sent": arb_alerts_sent,
        "tracked_bets": len(history),
    }


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    result = run_worker(dry_run=dry_run)
    if not result.get("success", False):
        sys.exit(1)


if __name__ == "__main__":
    main()
