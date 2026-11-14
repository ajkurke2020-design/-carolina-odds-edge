import math
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util import connection as urllib3_connection

from scanner_config import CONFIG

API_BASE_URL = "https://api.prop-line.com/v1"
API_MARKETS = "h2h,spreads,totals"
EASTERN = ZoneInfo("America/New_York")


class _IPv4HTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        try:
            address = socket.getaddrinfo(
                self._dns_host, self.port, socket.AF_INET, socket.SOCK_STREAM
            )[0][4][0]
            return urllib3_connection.create_connection(
                (address, self.port),
                self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )
        except socket.gaierror as error:
            raise NameResolutionError(self.host, self, error) from error
        except socket.timeout as error:
            raise ConnectTimeoutError(
                self, f"Connection to {self.host} timed out."
            ) from error
        except OSError as error:
            raise NewConnectionError(
                self, f"Failed to establish an IPv4 connection: {error}"
            ) from error


class _IPv4HTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _IPv4HTTPSConnection


class _PropLineIPv4Adapter(HTTPAdapter):
    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(
            self.poolmanager.pool_classes_by_scheme
        )
        self.poolmanager.pool_classes_by_scheme["https"] = _IPv4HTTPSConnectionPool


def _new_api_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://api.prop-line.com/", _PropLineIPv4Adapter())
    return session


SPORT_CONFIG = {
    "NFL": "football_nfl",
    "NCAAF": "football_ncaaf",
    "NBA": "basketball_nba",
    "NCAAB": "basketball_ncaab",
    "MLB": "baseball_mlb",
    "NHL": "hockey_nhl",
    "Tennis": "tennis",
}

APPROVED_BOOKMAKERS = {
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
}


def american_to_decimal(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    price = float(value)
    if not math.isfinite(price) or price == 0:
        return None
    return 1 + (price / 100 if price > 0 else 100 / abs(price))


def _market_team(market: dict[str, Any], outcome: dict[str, Any]) -> str | None:
    team = market.get("team")
    if not isinstance(team, str) or not team.strip():
        team = outcome.get("team")
    if not isinstance(team, str) or not team.strip():
        return None
    return team.strip()


def _market_identity(market_key: str, point: Any, team: str | None = None) -> str:
    if point is None:
        identity = market_key
    else:
        try:
            number = float(point)
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number):
            identity = market_key
        else:
            if market_key == "spreads":
                number = abs(number)
            identity = f"{market_key}:{number:g}"
    if market_key == "totals" and team:
        identity += f":team:{team.casefold()}"
    return identity


def _market_label(market_key: str, point: Any, team: str | None = None) -> str:
    if point is None:
        label = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(
            market_key, market_key.replace("_", " ").title()
        )
    else:
        try:
            line = float(point)
        except (TypeError, ValueError):
            line = math.nan
        if not math.isfinite(line):
            label = market_key.replace("_", " ").title()
        elif market_key == "spreads":
            label = f"Spread {line:+g}"
        elif market_key == "totals":
            label = f"Total {line:g}"
        else:
            label = market_key.replace("_", " ").title()
    if market_key == "totals" and team:
        label += f" ({team} team)"
    return label


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _is_certificate_validation_error(error: requests.exceptions.SSLError) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "certificate verify failed",
            "hostname mismatch",
            "self-signed certificate",
            "unable to get local issuer certificate",
        )
    )


def _event_is_in_mode(event: dict[str, Any], mode: str, now: datetime) -> bool:
    is_live = event.get("live") is True
    if event.get("completed") is True:
        return False
    if mode == "live":
        return is_live
    if is_live:
        return False
    if now.tzinfo is None:
        return False
    commence_time = event.get("commence_time")
    if not isinstance(commence_time, str):
        return False
    try:
        commence = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        if commence.tzinfo is None:
            return False
        return commence > now
    except ValueError:
        return False


def parse_odds_payload(
    events: list[dict[str, Any]], sport_label: str
) -> list[dict[str, Any]]:
    quotes_by_identity: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    accepted_bookmakers = set(APPROVED_BOOKMAKERS) | {
        CONFIG.pinnacle_bookmaker_key
    }
    for event in events:
        event_id = event.get("id")
        home = event.get("home_team")
        away = event.get("away_team")
        if (
            event_id is None
            or not isinstance(home, str)
            or not home.strip()
            or not isinstance(away, str)
            or not away.strip()
        ):
            continue

        bookmaker_rows = event.get("bookmakers")
        if not isinstance(bookmaker_rows, list):
            continue

        event_markets: dict[tuple[str, str, str], set[str]] = {}
        latest_market_updates: dict[tuple[str, str, str], datetime] = {}
        for bookmaker in bookmaker_rows:
            if (
                not isinstance(bookmaker, dict)
                or str(bookmaker.get("key", "")).casefold() not in accepted_bookmakers
            ):
                continue
            bookmaker_key = str(bookmaker["key"]).casefold()
            bookmaker_markets = bookmaker.get("markets")
            if not isinstance(bookmaker_markets, list):
                continue
            for market in bookmaker_markets:
                if not isinstance(market, dict):
                    continue
                market_key = market.get("key")
                if market_key not in {"h2h", "spreads", "totals"}:
                    continue
                for outcome in market.get("outcomes", []):
                    if not isinstance(outcome, dict):
                        continue
                    team = _market_team(market, outcome)
                    group_key = _market_identity(
                        market_key, outcome.get("point"), team
                    )
                    period = str(market.get("period") or "")
                    event_markets.setdefault(
                        (bookmaker_key, group_key, period), set()
                    ).add(
                        str(outcome.get("name", ""))
                    )
                    update = _parse_timestamp(market.get("last_update"))
                    if update is not None:
                        update_key = (bookmaker_key, group_key, period)
                        latest_market_updates[update_key] = max(
                            update, latest_market_updates.get(update_key, update)
                        )

        for bookmaker in bookmaker_rows:
            if (
                not isinstance(bookmaker, dict)
                or str(bookmaker.get("key", "")).casefold() not in accepted_bookmakers
            ):
                continue
            bookmaker_key = str(bookmaker["key"]).casefold()
            sportsbook = APPROVED_BOOKMAKERS.get(
                bookmaker_key, CONFIG.pinnacle_bookmaker_name
            )
            bookmaker_markets = bookmaker.get("markets")
            if not isinstance(bookmaker_markets, list):
                continue
            for market in bookmaker_markets:
                if not isinstance(market, dict):
                    continue
                market_key = market.get("key")
                if market_key not in {"h2h", "spreads", "totals"}:
                    continue
                for outcome in market.get("outcomes", []):
                    if not isinstance(outcome, dict):
                        continue
                    selection = outcome.get("name")
                    decimal_odds = american_to_decimal(outcome.get("price"))
                    if not isinstance(selection, str) or decimal_odds is None:
                        continue

                    team = _market_team(market, outcome)
                    point = outcome.get("point")
                    group_key = _market_identity(market_key, point, team)
                    period = str(market.get("period") or "")
                    observed = event_markets.get(
                        (bookmaker_key, group_key, period), set()
                    )
                    update = _parse_timestamp(market.get("last_update"))
                    latest_update = latest_market_updates.get(
                        (bookmaker_key, group_key, period)
                    )
                    if latest_update is not None and update != latest_update:
                        continue
                    last_seen = _parse_timestamp(outcome.get("last_seen_at"))
                    if update is not None and last_seen is not None and last_seen < update:
                        continue

                    if market_key in {"h2h", "spreads"}:
                        side = outcome.get("side")
                        if side == "home":
                            selection = home
                        elif side == "away":
                            selection = away
                        elif side == "draw":
                            selection = "Draw"
                        elif selection.casefold() == home.casefold():
                            selection = home
                        elif selection.casefold() == away.casefold():
                            selection = away
                        elif selection.casefold() == "draw":
                            selection = "Draw"

                    if market_key == "h2h":
                        expected = [home, away]
                        if any(name.casefold() == "draw" for name in observed):
                            expected.append("Draw")
                    elif market_key == "spreads":
                        expected = [home, away]
                    elif market_key == "totals":
                        expected = ["Over", "Under"]
                        if selection.casefold() == "over":
                            selection = "Over"
                        elif selection.casefold() == "under":
                            selection = "Under"
                    else:
                        expected = sorted(observed)

                    quote = {
                        "event_id": str(event_id),
                        "sport": sport_label,
                        "league": event.get("sport_title") or "",
                        "matchup": f"{home} vs {away}",
                        "kickoff": event.get("commence_time") or "",
                        "is_live": event.get("live") is True,
                        "completed": event.get("completed") is True,
                        "match_link": "",
                        "market": _market_label(market_key, point, team),
                        "market_key": group_key,
                        "period": period,
                        "team": team,
                        "point": point,
                        "expected_outcomes": expected,
                        "selection": selection,
                        "sportsbook": sportsbook,
                        "bookmaker_key": bookmaker_key,
                        "odds_american": outcome.get("price"),
                        "odds_decimal": decimal_odds,
                        "price_format": "american",
                        "liquidity": outcome.get("liquidity"),
                        "liquidity_updated_at": outcome.get(
                            "liquidity_updated_at"
                        ),
                        "payout_multiplier": outcome.get("payout_multiplier"),
                        "last_update": market.get("last_update"),
                        "last_change_at": outcome.get("last_change_at"),
                        "last_seen_at": outcome.get("last_seen_at"),
                        "book_updated_at": bookmaker.get("last_update"),
                    }
                    identity = (
                        str(event_id),
                        bookmaker_key,
                        group_key,
                        period,
                        selection,
                    )
                    current = quotes_by_identity.get(identity)
                    current_update = (
                        _parse_timestamp(current.get("last_update"))
                        if current
                        else None
                    )
                    if (
                        current is None
                        or (update is not None and current_update is None)
                        or (
                            update is not None
                            and current_update is not None
                            and update > current_update
                        )
                        or (
                            update == current_update
                            and decimal_odds > current["odds_decimal"]
                        )
                    ):
                        quotes_by_identity[identity] = quote
    return list(quotes_by_identity.values())


def _fetch_sport(
    sport_label: str, mode: str, api_key: str
) -> dict[str, Any]:
    result: dict[str, Any] = {"quotes": [], "errors": [], "warnings": [], "quota": {}}
    sport_key = SPORT_CONFIG[sport_label]
    session = _new_api_session()
    timeout_retries = 0
    try:
        request_args = {
            "params": {
                "markets": API_MARKETS,
                "bookmakers": ",".join(
                    [*APPROVED_BOOKMAKERS, CONFIG.pinnacle_bookmaker_key]
                ),
                "max_age": CONFIG.max_quote_age_seconds,
            },
            "headers": {"X-API-Key": api_key.strip()},
            "timeout": CONFIG.api_timeout_seconds,
        }
        url = f"{API_BASE_URL}/sports/{sport_key}/odds"
        tls_retries = 0
        while True:
            try:
                response = session.get(url, **request_args)
                break
            except requests.exceptions.SSLError as error:
                if (
                    tls_retries == CONFIG.tls_retries
                    or _is_certificate_validation_error(error)
                ):
                    raise
                tls_retries += 1
                session.close()
                session = _new_api_session()
                time.sleep(0.5 * (2 ** (tls_retries - 1)))
            except requests.exceptions.Timeout:
                if timeout_retries == CONFIG.api_timeout_retries:
                    raise
                timeout_retries += 1
                session.close()
                session = _new_api_session()
                time.sleep(0.5 * (2 ** (timeout_retries - 1)))

        for header_name in ("X-Daily-Remaining", "X-Daily-Limit"):
            if response.headers.get(header_name) is not None:
                result["quota"][header_name] = response.headers[header_name]

        if response.status_code == 401:
            result["errors"].append(
                f"{sport_label}: PropLine rejected the API key (HTTP 401)."
            )
            return result
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            detail = f" Retry after {retry_after} seconds." if retry_after else ""
            result["errors"].append(
                f"{sport_label}: PropLine rate or daily quota limit reached (HTTP 429)."
                f"{detail} No automatic retry was made."
            )
            return result
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            result["errors"].append(
                f"{sport_label}: PropLine returned invalid JSON: {error}."
            )
            return result
        if not isinstance(payload, list) or not all(
            isinstance(event, dict) for event in payload
        ):
            result["errors"].append(
                f"{sport_label}: PropLine returned an invalid odds response."
            )
            return result

        matching_events = [
            event
            for event in payload
            if _event_is_in_mode(event, mode, datetime.now(EASTERN))
        ]
        result["quotes"] = parse_odds_payload(matching_events, sport_label)
        if not result["quotes"]:
            result["warnings"].append(
                f"{sport_label}: No {mode} odds were returned for the selected markets."
            )
    except requests.exceptions.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        detail = ""
        if error.response is not None:
            try:
                body = error.response.json()
                if isinstance(body, dict):
                    detail = body.get("message") or body.get("error") or ""
            except ValueError:
                detail = error.response.text.strip()
        result["errors"].append(
            f"{sport_label}: PropLine request failed (HTTP {status})."
            + (f" {detail}" if detail else "")
        )
    except requests.exceptions.SSLError as error:
        if _is_certificate_validation_error(error):
            result["errors"].append(
                f"{sport_label}: TLS certificate validation failed. "
                "Check your system certificate store, VPN, proxy, or HTTPS inspection "
                "settings. Certificate verification remains enabled."
            )
        else:
            result["errors"].append(
                f"{sport_label}: TLS handshake failed after "
                f"{CONFIG.tls_retries + 1} attempts; no HTTP response "
                f"was received ({error}). Check your network's IPv6 route, VPN, "
                "proxy, or firewall. "
                "Certificate verification remains enabled."
            )
    except requests.exceptions.Timeout as error:
        result["errors"].append(
            f"{sport_label}: PropLine request timed out after "
            f"{timeout_retries + 1} attempt(s) "
            f"(timeout={CONFIG.api_timeout_seconds}s): {error}."
        )
    except requests.exceptions.RequestException as error:
        result["errors"].append(f"{sport_label}: Could not reach PropLine: {error}.")
    finally:
        session.close()
    return result


def run_api(
    sports: list[str], mode: str = "upcoming", api_key: str = ""
) -> dict[str, Any]:
    if mode not in {"upcoming", "live"}:
        raise ValueError("Mode must be 'upcoming' or 'live'.")
    invalid_sports = [sport for sport in sports if sport not in SPORT_CONFIG]
    if invalid_sports:
        raise ValueError(f"Unsupported sports: {', '.join(invalid_sports)}")
    if not sports:
        return {
            "quotes": [],
            "errors": ["Select at least one sport to scan."],
            "warnings": [],
            "request_count": 0,
            "quota": {},
        }
    if not api_key.strip():
        return {
            "quotes": [],
            "errors": ["Enter your PropLine API key to scan odds."],
            "warnings": [],
            "request_count": 0,
            "quota": {},
        }

    max_workers = max(1, min(CONFIG.api_max_workers, len(sports)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(
            executor.map(
                lambda sport: _fetch_sport(sport, mode, api_key),
                sports,
            )
        )

    quotes = [quote for result in results for quote in result["quotes"]]
    errors = [error for result in results for error in result["errors"]]
    warnings = [warning for result in results for warning in result["warnings"]]
    quota: dict[str, str] = {}
    for result in results:
        for header, value in result["quota"].items():
            if header == "X-Daily-Remaining" and header in quota:
                try:
                    value = str(min(int(value), int(quota[header])))
                except ValueError:
                    pass
            quota[header] = value
    return {
        "quotes": quotes,
        "errors": errors,
        "warnings": warnings,
        "request_count": len(sports),
        "quota": quota,
    }
