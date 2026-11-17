"""Estimate line-adjustment standard deviations from stored Pinnacle snapshots.

For one selection at two Pinnacle lines L1 and L2 with fair probabilities p1 and
p2, a normal model gives sd = |L2 - L1| / |Phi^-1(p2) - Phi^-1(p1)|. This script
takes the median of those estimates per (sport, market) and reports sample counts.

Usage: python calibrate_sd.py [--db PATH]
"""

import argparse
import sqlite3
from collections import defaultdict
from contextlib import closing
from statistics import NormalDist, median

import database

SUPPORTED_MARKETS = ("spreads", "totals")


def implied_sd(line1: float, p1: float, line2: float, p2: float) -> float | None:
    if not (0 < p1 < 1 and 0 < p2 < 1) or line1 == line2:
        return None
    denominator = abs(NormalDist().inv_cdf(p2) - NormalDist().inv_cdf(p1))
    if denominator < 1e-9:
        return None
    return abs(line2 - line1) / denominator


def estimate_sds(rows: list[dict]) -> list[tuple[str, str, float]]:
    """Return (sport, market, sd) for each adjacent pair of lines per event.

    Each row needs: sport, event_id, market, period, team, selection, line,
    fair_probability, quote_time. Only one side of a two-way market is used
    (the alphabetically first selection) so mirrored sides are not counted twice.
    Where one line has several snapshots, the latest quote is used.
    """
    groups: dict[tuple, dict[str, dict[float, dict]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    selections: dict[tuple, set[str]] = defaultdict(set)
    for row in rows:
        if row["market"] not in SUPPORTED_MARKETS or row["line"] is None:
            continue
        group = (row["sport"], row["event_id"], row["market"], row["period"], row["team"])
        selections[group].add(row["selection"])
        current = groups[group][row["selection"]].get(row["line"])
        if current is None or (row["quote_time"] or "") > (current["quote_time"] or ""):
            groups[group][row["selection"]][row["line"]] = row

    estimates = []
    for group, by_selection in groups.items():
        chosen = min(selections[group])
        points = sorted(by_selection.get(chosen, {}).items())
        for (line1, row1), (line2, row2) in zip(points, points[1:]):
            sd = implied_sd(line1, row1["fair_probability"], line2, row2["fair_probability"])
            if sd is not None:
                estimates.append((group[0], group[2], sd))
    return estimates


def summarize(estimates: list[tuple[str, str, float]]) -> dict[tuple[str, str], dict]:
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sport, market, sd in estimates:
        buckets[(sport, market)].append(sd)
    return {
        key: {"median_sd": median(values), "samples": len(values)}
        for key, values in sorted(buckets.items())
    }


def load_snapshot_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT sport, event_id, market_key, period, team, selection, line,
               fair_probability, quote_time
        FROM pinnacle_market_snapshots
        WHERE line IS NOT NULL
        """
    ).fetchall()
    return [
        {
            **dict(row),
            "market": str(row["market_key"] or "").split(":", 1)[0].casefold(),
        }
        for row in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=database.DB_PATH)
    args = parser.parse_args()
    with closing(sqlite3.connect(args.db)) as conn:
        summary = summarize(estimate_sds(load_snapshot_rows(conn)))
    if not summary:
        print("No events with two or more Pinnacle lines on the same selection yet.")
        return
    print(f"{'sport':<8}{'market':<10}{'median sd':>12}{'samples':>10}")
    for (sport, market), stats in summary.items():
        print(f"{sport:<8}{market:<10}{stats['median_sd']:>12.3f}{stats['samples']:>10}")


if __name__ == "__main__":
    main()
