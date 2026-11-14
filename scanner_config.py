from dataclasses import dataclass


@dataclass(frozen=True)
class ScannerConfig:
    pinnacle_bookmaker_key: str = "pinnacle"
    pinnacle_bookmaker_name: str = "Pinnacle"
    devig_method: str = "power"
    min_ev_pct: float = 2.0
    verify_ev_pct: float = 15.0
    max_quote_age_seconds: int = 60
    max_pinnacle_overround: float = 1.06
    bankroll: float = 1000.0
    kelly_fraction: float = 0.25
    max_bet_pct: float = 0.01
    max_event_pct: float = 0.03
    enable_line_adjusted: bool = False
    max_line_diff: float = 1.5
    line_adjusted_kelly_fraction: float | None = None
    line_adjusted_stddev_by_sport: tuple[tuple[str, float], ...] = (
        ("NFL", 13.0),
        ("NCAAF", 17.0),
        ("NBA", 15.0),
        ("NCAAB", 14.0),
        ("MLB", 2.8),
        ("NHL", 1.8),
        ("Tennis", 1.0),
    )
    football_key_number_probability_boosts: tuple[tuple[int, float], ...] = (
        (3, 0.04),
        (7, 0.025),
    )
    api_timeout_seconds: int = 30
    api_timeout_retries: int = 1
    tls_retries: int = 4
    api_max_workers: int = 3
    arbitrage_rounding_step: int = 10

    @property
    def effective_line_adjusted_kelly_fraction(self) -> float:
        if self.line_adjusted_kelly_fraction is not None:
            return self.line_adjusted_kelly_fraction
        return self.kelly_fraction / 2


CONFIG = ScannerConfig()
DEVIG_METHODS = frozenset({"power", "shin", "multiplicative"})
