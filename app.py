import hmac
import os
from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd
import streamlit as st

from database import (
    finalize_started_opportunities,
    init_db,
    load_bets,
    load_opportunity_log,
    log_bet,
    log_scan,
    update_bet_result,
)
from ev_engine import calculate_capped_kelly_stakes, decimal_to_american
from scanner_logic import (
    expected_value_pct,
    deduplicate_alt_line_ladders,
    filter_quotes,
    find_arbitrage_opportunities,
    scan_pinnacle_ev,
)
from propline_api import APPROVED_BOOKMAKERS, EASTERN, SPORT_CONFIG, run_api
from scanner_config import CONFIG


def _configured_secret(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value.strip()
    try:
        value = st.secrets.get(name, "")
    except FileNotFoundError:
        return ""
    return value.strip() if isinstance(value, str) else ""


def _require_app_access() -> None:
    access_password = _configured_secret("APP_ACCESS_PASSWORD")
    if not access_password:
        st.error(
            "App access is disabled. Configure APP_ACCESS_PASSWORD in the server "
            "environment or Streamlit secrets."
        )
        st.stop()
    if len(access_password) < 16:
        st.error("APP_ACCESS_PASSWORD must contain at least 16 characters.")
        st.stop()

    if st.session_state.get("app_authenticated"):
        if st.sidebar.button("Log out"):
            st.session_state.clear()
            st.rerun()
        return

    st.title("NC Odds Edge")
    with st.form("app_access"):
        entered_password = st.text_input("Access passphrase", type="password")
        submitted = st.form_submit_button("Unlock")
    if submitted:
        if hmac.compare_digest(entered_password, access_password):
            st.session_state["app_authenticated"] = True
            st.rerun()
        st.error("Incorrect access passphrase.")
    st.stop()


st.set_page_config(page_title="NC Odds Edge", layout="wide")
_require_app_access()
init_db()
finalize_started_opportunities()

SPORTS = list(SPORT_CONFIG)
DEFAULT_BOOKS = list(APPROVED_BOOKMAKERS.values())


def _quote_matches_schedule(quote: dict, mode: str, now: datetime) -> bool:
    if quote.get("completed") is True:
        return False
    if mode == "Live":
        return quote.get("is_live") is True
    if quote.get("is_live") is True:
        return False
    kickoff = quote.get("kickoff")
    if not isinstance(kickoff, str):
        return False
    try:
        commence = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
    except ValueError:
        return False
    return commence.tzinfo is not None and commence > now


def _format_kickoff_et(value: str) -> str:
    if not isinstance(value, str):
        return "Unknown"
    try:
        kickoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Unknown"
    if kickoff.tzinfo is None:
        return "Unknown"
    return kickoff.astimezone(EASTERN).strftime("%a %b %d, %I:%M %p ET").replace(
        " 0", " "
    )


def _format_quote_time_et(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        return "Unavailable"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "Unavailable"
    if timestamp.tzinfo is None:
        return "Unavailable"
    return timestamp.astimezone(EASTERN).strftime("%b %d %I:%M:%S %p ET").replace(
        " 0", " "
    )


st.markdown(
    """
<style>
.stApp { background: #0a101b; color: #e7edf6; }
[data-testid="stSidebar"] { background: #101927; border-right: 1px solid #202c3d; }
.block-container { max-width: 1540px; padding-top: 1.5rem; }
[data-testid="stMetric"] {
    background: #111d2d; border: 1px solid #263449; border-radius: 10px;
    padding: 14px 16px;
}
[data-testid="stMetricLabel"] { color: #94a6bd; }
[data-testid="stMetricValue"] { color: #f0f5fb; }
div.stButton > button[kind="primary"] {
    background: #16a765; border-color: #16a765; color: #07140e; font-weight: 700;
}
div.stButton > button { border-radius: 8px; }
.edge-pill {
    display: inline-block; border: 1px solid #2d7657; border-radius: 20px;
    background: #102a22; color: #63dfa2; padding: 5px 10px;
    font-size: 0.78rem; font-weight: 700; letter-spacing: 0.04em;
}
.muted-note { color: #96a6bb; font-size: 0.9rem; }
[data-testid="stDataFrame"] { border: 1px solid #243246; border-radius: 8px; }
</style>
""",
    unsafe_allow_html=True,
)

if "quotes" not in st.session_state:
    st.session_state["quotes"] = []
if "scan_errors" not in st.session_state:
    st.session_state["scan_errors"] = []
if "scan_warnings" not in st.session_state:
    st.session_state["scan_warnings"] = []
if "scan_diagnostics" not in st.session_state:
    st.session_state["scan_diagnostics"] = None
if "ev_candidates" not in st.session_state:
    st.session_state["ev_candidates"] = []
if "last_scan" not in st.session_state:
    st.session_state["last_scan"] = None
if "quota" not in st.session_state:
    st.session_state["quota"] = {}
if "selected_books" not in st.session_state:
    st.session_state["selected_books"] = DEFAULT_BOOKS
if "pending_book_selection" in st.session_state:
    st.session_state["selected_books"] = st.session_state.pop("pending_book_selection")

quota_status = st.session_state.get("quota") or {}
quota_remaining = quota_status.get("X-Daily-Remaining")
quota_limit = quota_status.get("X-Daily-Limit")
if quota_remaining is None:
    quota_text = "Not reported"
elif quota_limit is None:
    quota_text = str(quota_remaining)
else:
    quota_text = f"{quota_remaining} of {quota_limit}"
last_scan_status = st.session_state.get("last_scan")
last_scan_time = (
    last_scan_status.get("time", "Unavailable")
    if last_scan_status
    else "Not scanned yet"
)
with st.sidebar.container(border=True):
    st.markdown("**System status**")
    scan_mode = st.segmented_control(
        "Scan mode (US Eastern)",
        ["Upcoming", "Live"],
        default="Upcoming",
        key="scan_mode",
    )
    st.caption(f"PropLine quota remaining: {quota_text}")
    st.caption(f"Last scan: {last_scan_time}")

st.sidebar.header("Scan setup")
selected_sports = st.sidebar.multiselect("Sports", SPORTS, default=SPORTS)

st.session_state["quotes"] = [
    quote
    for quote in st.session_state["quotes"]
    if quote.get("sportsbook") in DEFAULT_BOOKS
]
all_quotes = st.session_state["quotes"]
available_books = sorted(
    set(DEFAULT_BOOKS) | {quote["sportsbook"] for quote in all_quotes}
)
st.session_state["selected_books"] = [
    book
    for book in st.session_state["selected_books"]
    if book in available_books
]
selected_books = st.sidebar.multiselect(
    "Betting venues",
    available_books,
    key="selected_books",
)

bankroll = st.sidebar.number_input(
    "Bankroll ($)", min_value=0.0, value=CONFIG.bankroll, step=100.0
)
kelly_fraction = st.sidebar.slider(
    "Kelly fraction", min_value=0.1, max_value=0.5, value=CONFIG.kelly_fraction, step=0.05
)
line_adjusted_kelly_fraction = (
    kelly_fraction / 2
    if CONFIG.line_adjusted_kelly_fraction is None
    else CONFIG.line_adjusted_kelly_fraction
)
arb_stake = st.sidebar.number_input(
    "Arbitrage total stake ($)", min_value=1, value=100, step=10
)
arb_rounding = st.sidebar.selectbox(
    "Round arbitrage stakes to ($)",
    [1, 5, 10, 25],
    index=[1, 5, 10, 25].index(CONFIG.arbitrage_rounding_step),
)
st.sidebar.caption(
    f"Enabled venues: {', '.join(DEFAULT_BOOKS)}. Fair % requires a matching "
    f"Pinnacle market. Minimum +EV: {CONFIG.min_ev_pct:.1f}%. Kelly suggestions are "
    f"capped at {CONFIG.max_bet_pct:.0%} per bet and "
    f"{CONFIG.max_event_pct:.0%} per event. Line-adjusted pricing is "
    f"{'enabled' if CONFIG.enable_line_adjusted else 'disabled'}."
)

header, scan_action = st.columns([4, 1])
with header:
    st.title("NC Odds Edge")
    st.caption("Odds comparison, estimated value, and arbitrage opportunities")
with scan_action:
    st.markdown('<span class="edge-pill">PROPLINE API</span>', unsafe_allow_html=True)
    scan_button = st.button("Scan odds", type="primary", width="stretch")

server_api_key = _configured_secret("PROPLINE_API_KEY")
if server_api_key:
    api_key = server_api_key
    st.sidebar.caption("Using server-configured PropLine API credentials.")
else:
    api_key = st.sidebar.text_input(
        "PropLine API key",
        value="",
        type="password",
        help="Get a free key at prop-line.com. It is sent only to PropLine and is not shown.",
    )

st.info(
    "Odds are collected from PropLine in bulk by selected sport. Quotes can move or disappear; "
    "upcoming games must start after the current US Eastern time. "
    "Confirm availability, fees, and settlement rules at the venue before acting."
)

if scan_button:
    requested_mode = "live" if scan_mode == "Live" else "upcoming"
    with st.spinner("Pulling selected sports from PropLine..."):
        result = run_api(selected_sports, requested_mode, api_key)
    scan_result = scan_pinnacle_ev(result["quotes"])
    all_ev_rows = scan_result["opportunities"]
    for row in scan_result["eligible_rows"]:
        if row["pricing_method"] == "line-adjusted":
            row["kelly_fraction"] = line_adjusted_kelly_fraction
    scan_stakes = calculate_capped_kelly_stakes(
        all_ev_rows, bankroll, kelly_fraction
    )
    for row, stake in zip(all_ev_rows, scan_stakes):
        row["kelly_stake"] = stake
    scan_id = str(uuid4())
    observed_at = datetime.now(timezone.utc).isoformat()
    log_scan(
        scan_id,
        observed_at,
        all_ev_rows,
        scan_result["pinnacle_snapshots"],
    )
    st.session_state["quotes"] = [
        quote
        for quote in result["quotes"]
        if quote.get("sportsbook") in DEFAULT_BOOKS
    ]
    st.session_state["ev_candidates"] = scan_result["eligible_rows"]
    st.session_state["scan_diagnostics"] = scan_result["diagnostics"]
    st.session_state["scan_errors"] = result["errors"]
    st.session_state["scan_warnings"] = result["warnings"]
    st.session_state["request_count"] = result["request_count"]
    st.session_state["quota"] = result["quota"]
    st.session_state["last_scan"] = {
        "mode": scan_mode,
        "time": datetime.now(EASTERN).strftime("%Y-%m-%d %I:%M:%S %p ET"),
    }
    if st.session_state["quotes"]:
        st.session_state["pending_book_selection"] = sorted(
            {quote["sportsbook"] for quote in st.session_state["quotes"]}
        )
    st.rerun()

last_scan = st.session_state["last_scan"]
stored_quotes = st.session_state["quotes"]
if last_scan and last_scan["mode"] != scan_mode:
    all_quotes = []
    st.info(
        f"The saved snapshot is from {last_scan['mode']}. Scan again to load "
        f"{scan_mode.lower()} events."
    )
else:
    now_eastern = datetime.now(EASTERN)
    all_quotes = [
        quote
        for quote in stored_quotes
        if _quote_matches_schedule(quote, scan_mode, now_eastern)
    ]

for message in st.session_state["scan_errors"]:
    st.error(message)
for message in st.session_state["scan_warnings"]:
    st.warning(message)

quota = st.session_state.get("quota", {})
if quota:
    remaining = quota.get("X-Daily-Remaining")
    daily_limit = quota.get("X-Daily-Limit")
    if remaining is not None and daily_limit is not None:
        st.caption(f"PropLine daily API requests remaining: {remaining} of {daily_limit}")

if last_scan:
    if last_scan["mode"] != scan_mode:
        st.caption(
            f"Last scan: {last_scan['mode']} at {last_scan['time']}; "
            f"the current {scan_mode.lower()} view needs a new scan."
        )
    elif all_quotes:
        st.caption(
            f"{last_scan['mode']} snapshot · Updated {last_scan['time']} · "
            f"{len(all_quotes)} bookmaker quotes · "
            f"{st.session_state.get('request_count', 0)} API requests"
        )
    elif stored_quotes:
        st.caption(
            f"{last_scan['mode']} snapshot from {last_scan['time']} has no "
            "events still matching the selected schedule. Scan again for current odds."
        )
    else:
        st.caption(
            f"{last_scan['mode']} scan · {last_scan['time']} · No odds returned · "
            f"{st.session_state.get('request_count', 0)} API requests"
        )
else:
    st.caption("Run a scan to load real PropLine prices. No sample prices are shown.")

with st.container(horizontal=True, vertical_alignment="bottom"):
    matchup_search = st.text_input(
        "Search matchups",
        type="search",
        placeholder="Search teams or matchups",
        key="matchup_search",
    )
    minimum_ev_filter = st.slider(
        "Minimum EV %",
        min_value=float(CONFIG.min_ev_pct),
        max_value=100.0,
        value=float(CONFIG.min_ev_pct),
        step=0.5,
        format="%.1f%%",
        key="minimum_ev_filter",
    )

filtered_quotes = filter_quotes(all_quotes, selected_sports, selected_books)
search_term = matchup_search.strip().casefold()
if search_term:
    filtered_quotes = [
        quote
        for quote in filtered_quotes
        if search_term in str(quote.get("matchup") or "").casefold()
    ]
ev_candidates = filter_quotes(
    st.session_state.get("ev_candidates", []),
    selected_sports,
    selected_books,
)
visible_events = {
    (quote["sport"], quote["event_id"])
    for quote in all_quotes
}
ev_candidates = [
    quote
    for quote in ev_candidates
    if (quote["sport"], quote["event_id"]) in visible_events
]
if search_term:
    ev_candidates = [
        quote
        for quote in ev_candidates
        if search_term in str(quote.get("matchup") or "").casefold()
    ]
ev_opportunities, _ = deduplicate_alt_line_ladders(ev_candidates)
ev_opportunities = [
    quote
    for quote in ev_opportunities
    if quote["ev_pct"] >= minimum_ev_filter
]
ev_opportunities.sort(key=lambda quote: quote["ev_pct"], reverse=True)
for opportunity in ev_opportunities:
    if opportunity["pricing_method"] == "line-adjusted":
        opportunity["kelly_fraction"] = line_adjusted_kelly_fraction
kelly_stakes = calculate_capped_kelly_stakes(
    ev_opportunities, bankroll, kelly_fraction
)

arbitrage_opportunities = find_arbitrage_opportunities(
    filtered_quotes, arb_stake, arb_rounding
)

diagnostics = st.session_state.get("scan_diagnostics")
if diagnostics:
    with st.expander("Latest scan diagnostics", expanded=False):
        diag_columns = st.columns(4)
        diag_columns[0].metric("Fetched quote rows", diagnostics["rows_fetched"])
        diag_columns[1].metric("Bettable quote rows", diagnostics["bettable_quotes"])
        diag_columns[2].metric("Kept +EV rows", diagnostics["rows_kept"])
        distribution = diagnostics["ev_pct"]
        if distribution["min"] is None:
            ev_distribution = "No qualifying rows"
        else:
            ev_distribution = (
                f"min {distribution['min']:.2f}% · "
                f"median {distribution['median']:.2f}% · "
                f"max {distribution['max']:.2f}%"
            )
        diag_columns[3].metric("EV distribution", ev_distribution)
        dropped_labels = {
            "no_pinnacle_reference": "No Pinnacle reference",
            "one_sided_pinnacle_market": "One-sided Pinnacle market",
            "line_mismatch": "Line mismatch",
            "period_mismatch": "Period mismatch",
            "team_scope_mismatch": "Team/game-total mismatch",
            "stale_quote": "Stale quote",
            "overround_too_high": "Pinnacle overround too high",
            "started": "Event started",
            "missing_start_time": "Missing start time",
            "invalid_pinnacle_market": "Invalid Pinnacle market",
            "below_min_ev": "Below minimum EV",
            "below_min_liquidity": "Below minimum liquidity",
            "alt_line_duplicate": "Lower-EV alternate-line duplicate",
        }
        dropped = diagnostics["dropped"]
        st.dataframe(
            pd.DataFrame(
                [
                    {"Drop reason": label, "Rows": dropped[key]}
                    for key, label in dropped_labels.items()
                ]
            ),
            hide_index=True,
            width="stretch",
            alt="Latest scan drop reasons and quote counts",
        )

matchup_count = len({quote["event_id"] for quote in filtered_quotes})
metric_columns = st.columns(4)
metric_columns[0].metric("Matchups", matchup_count)
metric_columns[1].metric("+EV prices", len(ev_opportunities))
metric_columns[2].metric("Arbitrage markets", len(arbitrage_opportunities))
metric_columns[3].metric(
    "Venues", len({quote["sportsbook"] for quote in filtered_quotes})
)

tab_ev, tab_arb, tab_tracker, tab_guide = st.tabs(
    ["+EV board", "Arbitrage", "Bet tracker", "Terms guide"]
)

with tab_ev:
    st.subheader("Positive expected value")
    st.markdown(
        '<p class="muted-note">Exact-match fair probability uses a matching, two-sided '
        "Pinnacle market and the configured de-vig method. Line-adjusted pricing is "
        f"{'enabled' if CONFIG.enable_line_adjusted else 'disabled'}; when enabled, it "
        "interpolates bracketed Pinnacle lines or uses the configured distribution model "
        "within the maximum line difference. Pinnacle is a reference only and never appears "
        "as a bettable row. Quotes below the "
        f"{CONFIG.min_ev_pct:.1f}% minimum are excluded; edges over "
        f"{CONFIG.verify_ev_pct:.0f}% are flagged for verification rather than hidden. "
        "Kelly stakes are quarter-Kelly by default, capped per bet and per event. "
        "Use the quick filters above to narrow matchups and raise the minimum EV.</p>",
        unsafe_allow_html=True,
    )
    if not all_quotes:
        st.info("No odds loaded yet. Choose sports in the sidebar and run a scan.")
    elif not filtered_quotes:
        st.info("No quotes match the selected sports, venues, or matchup search.")
    elif not ev_opportunities:
        st.info(
            f"No selected quotes meet the {minimum_ev_filter:.1f}% minimum EV "
            "filter with an eligible Pinnacle reference."
        )
    else:
        table_rows = []
        for quote, recommended_stake in zip(ev_opportunities, kelly_stakes):
            table_rows.append(
                {
                    "Sport": quote["sport"],
                    "Matchup": quote["matchup"],
                    "Start (ET)": _format_kickoff_et(quote["kickoff"]),
                    "Market": f"{quote['market']} · {quote['period']}".strip(" ·"),
                    "Selection": quote["selection"],
                    "Book / venue": quote["sportsbook"],
                    "American": decimal_to_american(quote["odds_decimal"]),
                    "Decimal": float(quote["odds_decimal"]),
                    "EV %": float(quote["ev_pct"]),
                    "Kelly stake": float(recommended_stake),
                    "Liquidity": quote.get("liquidity"),
                    "Flags": quote["flags"],
                }
            )
        ev_table = pd.DataFrame(table_rows)
        ev_table["Liquidity"] = pd.to_numeric(
            ev_table["Liquidity"], errors="coerce"
        )
        max_ev_for_progress = max(20.0, float(ev_table["EV %"].max()))
        st.dataframe(
            ev_table,
            column_config={
                "Sport": st.column_config.TextColumn("Sport", width="small"),
                "Matchup": st.column_config.TextColumn(
                    "Matchup", width="medium", pinned=True
                ),
                "Start (ET)": st.column_config.TextColumn(
                    "Start (ET)", width="medium"
                ),
                "Market": st.column_config.TextColumn("Market", width="medium"),
                "Selection": st.column_config.TextColumn("Selection"),
                "Book / venue": st.column_config.TextColumn("Book / venue"),
                "American": st.column_config.NumberColumn(
                    "American", format="%+d", width="small"
                ),
                "Decimal": st.column_config.NumberColumn(
                    "Decimal", format="%.2f", width="small"
                ),
                "EV %": st.column_config.ProgressColumn(
                    "EV %",
                    format="%.2f%%",
                    min_value=0.0,
                    max_value=max_ev_for_progress,
                    color="green",
                    help="Estimated expected value as a percentage of stake.",
                ),
                "Kelly stake": st.column_config.NumberColumn(
                    "Kelly stake", format="$%.2f"
                ),
                "Liquidity": st.column_config.NumberColumn(
                    "Liquidity",
                    format="$%.2f",
                    help="Liquidity reported by the venue, when available.",
                ),
                "Flags": st.column_config.TextColumn("Flags", width="small"),
            },
            hide_index=True,
            width="stretch",
            alt="Positive expected value opportunities with odds and suggested stake",
        )
        st.download_button(
            "Download +EV opportunities (CSV)",
            data=ev_table.to_csv(index=False).encode("utf-8"),
            file_name="pinnacle_anchored_ev.csv",
            mime="text/csv",
        )
        st.caption(
            f"{kelly_fraction:.0%} Kelly, capped at "
            f"{CONFIG.max_bet_pct:.0%} per bet and "
            f"{CONFIG.max_event_pct:.0%} per event; line-adjusted rows use "
            f"{line_adjusted_kelly_fraction:.1%} Kelly; "
            "stakes round down to cents."
        )

with tab_arb:
    st.subheader("Arbitrage opportunities")
    st.caption(
        f"Quotes missing a timestamp or older than {CONFIG.max_quote_age_seconds} "
        "seconds are excluded. "
        "The saved snapshot does not auto-refresh; scan again before acting."
    )
    if not all_quotes:
        st.info("Run a scan to check whether selected venues offer all outcomes at arbitrage prices.")
    elif not filtered_quotes:
        st.info("No quotes match the selected sports and venues.")
    elif not arbitrage_opportunities:
        st.info(
            "No arbitrage survives the current venues and stake-rounding increment. "
            "Try a smaller rounding increment to check whether a theoretical edge remains profitable."
        )
    for opportunity in arbitrage_opportunities:
        with st.container(border=True):
            event_column, profit_column = st.columns([3, 1])
            event_column.markdown(
                f"**{opportunity['matchup']}** · {opportunity['sport']} · "
                f"{_format_kickoff_et(opportunity['kickoff'])} · "
                f"{opportunity['period'] or 'Full market'}"
            )
            profit_column.metric(
                "Projected gross profit",
                f"${opportunity['profit']:.2f}",
                f"{opportunity['roi_pct']:.2f}% after rounding",
            )
            event_column.caption(
                f"Stake ${opportunity['total_stake']:,.0f} in total · "
                f"minimum payout (including stakes) ${opportunity['locked_payout']:.2f} · "
                f"two different venues · L = "
                f"{opportunity['implied_probability_sum']:.4f}"
            )
            legs = st.columns(len(opportunity["legs"]))
            for index, leg in enumerate(opportunity["legs"]):
                legs[index].markdown(
                    f"**{leg['selection']}**  \n{leg['market']}  \n"
                    f"**{leg['sportsbook']}** · {leg['odds_decimal']:.2f} decimal "
                    f"({decimal_to_american(leg['odds_decimal']):+d} American)  \n"
                    f"Bet **${leg['stake']:,.0f}**  \n"
                    f"Payout if it wins (including stake) **${leg['payout']:.2f}**"
                )
                liquidity = leg.get("liquidity")
                if isinstance(liquidity, (int, float)) and not isinstance(
                    liquidity, bool
                ):
                    legs[index].caption(
                        f"Quote liquidity: ${liquidity:,.2f}"
                        + (
                            f" · updated {leg['liquidity_updated_at']}"
                            if leg.get("liquidity_updated_at")
                            else ""
                        )
                    )
                legs[index].caption(
                    f"Gross before fees: "
                    f"${leg['payout'] - opportunity['total_stake']:+.2f}"
                )
    if all_quotes:
        st.caption(
            "Each card tells you the stake for each outcome and the return if it wins. "
            "Only two-outcome opportunities with opposite spread points and a different "
            "venue on each leg are shown. The total stake is allocated using the selected "
            f"${arb_rounding} rounding increment. Confirm both bets are accepted at the displayed odds; limits, "
            "fees, price movement, insufficient quote liquidity, and differing settlement "
            "rules can remove the projected gross profit. Displayed liquidity is not used "
            "to cap stake sizes."
        )

with tab_tracker:
    st.subheader("Bet history and closing-line value")
    opportunity_log = load_opportunity_log()
    with st.expander("Scanned +EV history and CLV", expanded=False):
        st.caption(
            "Exact-match and line-adjusted opportunities are tracked in separate CLV "
            "sections. Exact rows use the same Pinnacle line; adjusted rows estimate "
            "Pinnacle's closing probability at the venue's line. This is an observed "
            "pregame close, not an official closing line; scan near kickoff for a closer estimate."
        )
        if opportunity_log.empty:
            st.info("No Pinnacle-referenced +EV opportunities have been logged yet.")
        else:
            for pricing_method, title in (
                ("exact", "Exact-match CLV"),
                ("line-adjusted", "Line-adjusted CLV"),
            ):
                method_log = opportunity_log[
                    opportunity_log["pricing_method"] == pricing_method
                ]
                st.markdown(f"**{title}**")
                if method_log.empty:
                    st.caption("No rows in this CLV bucket yet.")
                    continue
                closed_clv = method_log["clv_pct"].dropna()
                if closed_clv.empty:
                    st.caption(
                        f"{len(method_log)} logged; no closing Pinnacle reference recorded yet."
                    )
                else:
                    st.caption(
                        f"{len(method_log)} logged · {len(closed_clv)} with closing "
                        f"CLV · mean {closed_clv.mean():+.2f}%"
                    )
                st.dataframe(
                    method_log,
                    hide_index=True,
                    width="stretch",
                    alt=f"{method_label} positive EV and closing line history",
                )
            st.download_button(
                "Download +EV history and CLV (CSV)",
                data=opportunity_log.to_csv(index=False).encode("utf-8"),
                file_name="pinnacle_ev_clv_history.csv",
                mime="text/csv",
            )
    with st.expander("Log a real bet manually"):
        with st.form("manual_bet_form"):
            manual_sport = st.selectbox("Sport", SPORTS)
            manual_matchup = st.text_input("Matchup")
            manual_market = st.text_input("Market")
            manual_selection = st.text_input("Selection")
            manual_book = st.selectbox("Book / market venue", available_books)
            manual_odds = st.number_input(
                "Decimal odds", min_value=1.01, max_value=1000.0, value=2.0, step=0.01
            )
            manual_fair_probability_pct = st.number_input(
                "Your estimated fair probability (%)",
                min_value=0.1,
                max_value=99.9,
                value=50.0,
                step=0.5,
            )
            manual_stake = st.number_input(
                "Amount staked ($)", min_value=0.01, value=10.0, step=1.0
            )
            save_manual_bet = st.form_submit_button("Log real bet")
            if save_manual_bet:
                if not all(
                    [
                        manual_matchup.strip(),
                        manual_market.strip(),
                        manual_selection.strip(),
                    ]
                ):
                    st.error("Enter the matchup, market, and selection.")
                else:
                    manual_fair_probability = manual_fair_probability_pct / 100
                    log_bet(
                        {
                            "mode": "Manual entry",
                            "sport": manual_sport,
                            "matchup": manual_matchup.strip(),
                            "market": manual_market.strip(),
                            "selection": manual_selection.strip(),
                            "sportsbook": manual_book,
                            "odds_american": decimal_to_american(manual_odds),
                            "odds_decimal": manual_odds,
                            "fair_prob_at_bet": manual_fair_probability,
                            "expected_ev_pct": expected_value_pct(
                                manual_odds, manual_fair_probability
                            ),
                            "stake": manual_stake,
                        }
                    )
                    st.success("Logged your manually entered bet.")
                    st.rerun()

    bets_df = load_bets()
    if bets_df.empty:
        st.info("No bets logged yet.")
    else:
        total_staked = bets_df["stake"].sum()
        net_profit = bets_df["profit_loss"].sum()
        roi = net_profit / total_staked * 100 if total_staked > 0 else 0.0
        metric_columns = st.columns(4)
        metric_columns[0].metric("Bets logged", len(bets_df))
        metric_columns[1].metric("Net profit", f"${net_profit:.2f}")
        metric_columns[2].metric("ROI", f"{roi:.2f}%")
        metric_columns[3].metric(
            "Average logged EV", f"{bets_df['expected_ev_pct'].mean():.2f}%"
        )
        st.dataframe(
            bets_df,
            width="stretch",
            alt="Manually tracked bets and their results",
        )

        pending = bets_df[bets_df["status"] == "Pending"]
        if not pending.empty:
            st.markdown("#### Grade a pending bet")
            bet_id = st.selectbox("Bet ID", pending["id"].tolist())
            result = st.selectbox("Result", ["Won", "Lost", "Push"])
            closing_odds = st.number_input(
                "Closing decimal odds", min_value=1.01, value=2.0, step=0.01
            )
            if st.button("Save result"):
                bet = pending[pending["id"] == bet_id].iloc[0]
                if result == "Won":
                    profit_loss = bet["stake"] * (bet["odds_decimal"] - 1)
                elif result == "Lost":
                    profit_loss = -bet["stake"]
                else:
                    profit_loss = 0.0
                clv_pct = ((1 / closing_odds * bet["odds_decimal"]) - 1) * 100
                update_bet_result(
                    int(bet_id), result, closing_odds, profit_loss, clv_pct
                )
                st.success(f"Saved result for bet #{bet_id}.")
                st.rerun()

        st.download_button(
            "Download betting history (CSV)",
            data=bets_df.to_csv(index=False).encode("utf-8"),
            file_name="my_nc_bets.csv",
            mime="text/csv",
        )

with tab_guide:
    st.subheader("Plain-language guide")
    st.markdown(
        """
| Term | What it means |
|---|---|
| **American odds** | `+150` means a $100 win earns $150 profit; `-150` means risking $150 to earn $100 profit. |
| **Decimal odds** | Total return per $1 staked, including the original stake. `2.50` returns $2.50 on a $1 win. |
| **Implied probability** | The break-even win chance suggested by odds before accounting for venue margin or fees: `1 / decimal odds`. |
| **Fair probability** | An estimate of an outcome's chance. This app uses only a matching two-sided Pinnacle quote, removes its vig with the configurable power method by default, and applies that probability to other approved venues quoting the exact same event, period, market, team scope, line, and selection. Pinnacle is a reference, never a bettable row. It remains an estimate, not a guarantee. |
| **Vig (overround)** | The margin reflected by odds. Add each outcome's implied probability; a total above 100% is the overround. Prediction-market fees and bid/ask spreads are separate and are not deducted from EV. |
| **Pinnacle overround** | The sum of Pinnacle's two implied probabilities before de-vigging. Markets above the configured limit are excluded. |
| **Expected value (EV)** | Average theoretical profit or loss over many similar bets: `EV ($) = [P(win) x (decimal odds - 1) - P(loss)] x stake`. EV% is EV divided by stake; EV / $100 shows the expected dollars on a $100 wager. Equivalently, `EV% = (decimal odds x fair probability - 1) x 100`. Positive EV does not mean this bet will win. |
| **Kelly criterion** | A formula for how much of a bankroll to wager based on your estimated win chance and payout. In simple terms, a bigger edge suggests a larger stake, but Kelly is sensitive to probability errors. |
| **Kelly fraction** | The share of the full Kelly suggestion to use. `0.25` is quarter-Kelly. Suggestions are capped at 1% of bankroll per bet and 3% per event, then rounded down to cents; these safeguards do not remove risk. |
| **Arbitrage** | For a two-outcome market, take the opposite sides at two different venues when `1 / odds1 + 1 / odds2 < 1`. The app shows the stake and return for each side, plus the minimum profit after rounding. Odds movement, limits, rejection, fees, and differing settlement rules still create execution risk. |
| **Verify flag** | A Pinnacle-referenced edge above 15%. It is shown but flagged because stale, mismatched, or unusual odds are more likely at very large EV values. |
| **Closing-line value (CLV)** | Compares the logged book price with the latest stored Pinnacle fair probability before kickoff: `book decimal odds x closing fair probability - 1`. A scan near kickoff improves this estimate; it is not an official close unless the feed provides one. |
| **ROI** | Net profit divided by total amount staked. Results can vary widely over small samples. |

PropLine coverage and accepted prices vary. Verify that every wager is available at the quoted price and that fees, market period, and settlement rules match across venues. EV and arbitrage estimates do not subtract exchange fees or order-book spreads.
"""
    )
