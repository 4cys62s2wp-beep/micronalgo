"""Configuration.

Everything is settable by environment variable (prefix ``MICRONALGO_``) or a
``.env`` file, so no secret ever has to live in a config file inside the repo.

Timing philosophy
-----------------
Submission times are stored as **offsets from the session's own open and close**,
never as absolute wall-clock times. On a 13:00 ET half-day the closing auction
moves three hours earlier; an absolute "15:45" schedule would miss it entirely
and leave the account holding an unintended intraday position -- the exact
exposure with the negative expected return. Offsets are immune to that, and to
daylight saving, because the calendar supplies the session's real close.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MICRONALGO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Fields with a validation_alias (the ALPACA_* credentials) would
        # otherwise be settable ONLY by their alias, so load_settings(
        # alpaca_key_id=...) would silently produce an empty key.
        populate_by_name=True,
    )

    # ---------------------------------------------------------------- instrument
    symbol: str = "MU"

    # ---------------------------------------------------------------- broker
    broker: Literal["alpaca", "sim"] = "sim"
    alpaca_key_id: str = Field(default="", validation_alias="ALPACA_API_KEY_ID")
    alpaca_secret_key: str = Field(default="", validation_alias="ALPACA_API_SECRET_KEY")
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: Literal["iex", "sip"] = "iex"

    live_trading_ack: str = ""
    """Must equal ``I UNDERSTAND THIS IS REAL MONEY`` before a live base URL is
    accepted. A typo in a URL must never be sufficient to trade real money."""

    alpaca_paper: bool | None = None
    """Explicitly declare whether ``alpaca_base_url`` is a paper endpoint.

    ``None`` infers it from the hostname. The inference exists for convenience,
    not as a security boundary -- Alpaca operates more than one region (the EU
    entity among them) and hostnames differ, so a user whose paper endpoint is
    not recognised sets this to ``True`` and is done.

    This is deliberately separate from :attr:`live_trading_ack`: an
    unrecognised *paper* host must never be resolved by telling someone to
    acknowledge real-money trading. That would train exactly the wrong reflex.
    """

    # ---------------------------------------------------------------- sizing
    initial_capital: float = 100_000.0
    capital_fraction: float = 0.35
    """Anteil des Eigenkapitals, der in der Position steckt.

    0.35 ist kein runder Vorsichtswert, sondern das Ergebnis der
    Groessentabelle im Bericht: der groesste Anteil, der den historisch
    gemessenen Rueckgang von -54 % noch innerhalb der -30 %-Schwelle haelt
    (-23.0 %, zum Preis von 15.1 % statt 45.1 % pro Jahr).

    Frueher stand hier 0.95 -- gedacht als "fast alles einsetzen, 5 % Luft
    fuer Preisbewegungen zwischen Referenzkurs und Ausfuehrung". Das ist eine
    Antwort auf die Frage nach der Stueckzahl, nicht auf die nach dem Risiko,
    und es widersprach der Empfehlung, die derselbe Bericht ausspricht. Wer
    die volle Groesse will, setzt sie ausdruecklich."""
    """Fraction of account equity deployed per trade. Below 1.0 so a fill above
    the expected price cannot reject for insufficient buying power."""

    leverage: float = 1.0
    max_notional: float = 250_000.0
    max_shares: int = 100_000
    max_participation_of_adv: float = 0.005
    """Cap the order at this fraction of average daily volume. Retail size is
    nowhere near it; the cap exists so a config mistake cannot move the auction."""

    fractional_shares: bool = False
    """Auction (MOC/OPG) orders generally require whole shares. [verify-at-runtime]"""

    # ---------------------------------------------------------------- execution
    entry_order_type: Literal["moc", "loc"] = "moc"
    exit_order_type: Literal["opg_market", "opg_limit", "market_open"] = "opg_market"
    loc_limit_offset_bps: float = 50.0
    """For limit-on-close: how far through the last price to place the limit.
    Wide enough to fill in the auction, tight enough to reject a fat finger."""

    entry_submit_offset_min: int = 15
    """Submit the closing-auction order this many minutes before the close."""
    entry_cutoff_offset_min: int = 10
    """Hard cutoff. Alpaca rejects MOC/LOC submitted after ~15:50 ET on a normal
    day; a 10-minute pre-close cutoff stays inside that on half-days too.
    [verify-at-runtime]"""

    exit_submit_offset_min: int = 60
    """Submit the opening-auction order this many minutes before the open."""
    exit_cutoff_offset_min: int = 5
    """Alpaca rejects OPG orders submitted after ~09:28 ET. [verify-at-runtime]"""

    on_missed_entry: Literal["skip", "market_before_close"] = "skip"
    """If the closing-auction window is missed, the default is to skip the trade.
    Chasing it with a market order at 15:52 forfeits the auction print -- which
    is the entire reason the edge survives costs -- to capture one session's
    worth of a few basis points. Skipping costs a missed opportunity; chasing
    costs money and silently decouples live results from the backtest."""

    on_missed_exit: Literal["market_at_open", "hold_and_alert"] = "market_at_open"
    """If the opening-auction window is missed, sending a plain market order just
    after the open costs a spread. Holding costs a full session of the leg whose
    historical expected return is *negative*. The default pays the spread."""

    verify_after_open_min: int = 5
    verify_after_close_min: int = 5

    # ---------------------------------------------------------------- risk
    kill_switch_file: Path = Path("state/KILL")
    max_drawdown_halt: float = 0.25
    daily_loss_limit: float = 0.05
    max_consecutive_losses: int = 12
    max_price_deviation: float = 0.35
    """Refuse to trade if the reference price differs from the last known close
    by more than this. Catches a bad quote and an unapplied split alike."""
    max_data_age_days: int = 5
    api_error_budget: int = 8
    api_error_window_min: int = 30

    skip_earnings: bool = False
    earnings_csv: Path = Path("data/earnings_mu.csv")
    earnings_blackout_sessions: int = 0

    # ---------------------------------------------------------------- runtime
    dry_run: bool = True
    """Log intended orders without sending them. The default is *on*: turning it
    off must be a deliberate act."""

    state_dir: Path = Path("state")
    log_dir: Path = Path("logs")
    cache_dir: Path = Path("data/cache")
    reports_dir: Path = Path("reports")
    data_providers: tuple[str, ...] = ("stooq", "yahoo")

    poll_interval_sec: int = 20
    order_wait_timeout_sec: int = 300
    notify_webhook: str = ""
    log_level: str = "INFO"

    # ---------------------------------------------------------------- validation
    @field_validator("capital_fraction")
    @classmethod
    def _frac(cls, v: float) -> float:
        if not 0 < v <= 1.0:
            raise ValueError("capital_fraction must be in (0, 1]")
        return v

    @field_validator("leverage")
    @classmethod
    def _lev(cls, v: float) -> float:
        if not 0 < v <= 4.0:
            raise ValueError("leverage must be in (0, 4]")
        return v

    @staticmethod
    def _host_looks_like_paper(url: str) -> bool:
        """Is ``paper`` a label of this hostname?

        Matches ``paper-api.alpaca.markets``, ``paper.alpaca.markets`` and
        regional variants such as ``paper-api.eu.alpaca.markets``. Matching on
        labels rather than a bare substring keeps a host like
        ``notpaperapi.example.com`` from passing.
        """
        host = urlparse(url if "//" in url else f"https://{url}").hostname or ""
        return any(
            label == "paper" or label.startswith("paper-")
            for label in host.lower().split(".")
        )

    @model_validator(mode="after")
    def _guard_real_money(self) -> Settings:
        if self.broker != "alpaca":
            return self._guard_windows()

        is_paper = (
            self.alpaca_paper
            if self.alpaca_paper is not None
            else self._host_looks_like_paper(self.alpaca_base_url)
        )
        if not is_paper and self.live_trading_ack != "I UNDERSTAND THIS IS REAL MONEY":
            raise ValueError(
                f"alpaca_base_url {self.alpaca_base_url!r} is not recognised as a paper "
                "endpoint, so this refuses to start. Two very different situations:\n"
                "  (a) It IS a paper endpoint that this check does not know -- Alpaca runs "
                "several regions and the hostnames differ. Then set "
                "MICRONALGO_ALPACA_PAPER=true and carry on. Nothing else changes.\n"
                "  (b) It is a LIVE endpoint and you mean to trade real money. Then set "
                "MICRONALGO_LIVE_TRADING_ACK='I UNDERSTAND THIS IS REAL MONEY' -- and do "
                "not, until docs/GO_LIVE_CHECKLIST.md is fully satisfied.\n"
                "If you are unsure which one you have, it is (a): check the Alpaca dashboard, "
                "where paper and live accounts are separate and switched explicitly."
            )
        return self._guard_windows()

    def _guard_windows(self) -> Settings:
        if self.entry_cutoff_offset_min >= self.entry_submit_offset_min:
            raise ValueError("entry_cutoff_offset_min must be smaller than entry_submit_offset_min")
        if self.exit_cutoff_offset_min >= self.exit_submit_offset_min:
            raise ValueError("exit_cutoff_offset_min must be smaller than exit_submit_offset_min")
        return self

    # ---------------------------------------------------------------- helpers
    @property
    def is_paper(self) -> bool:
        if self.broker == "sim":
            return True
        if self.alpaca_paper is not None:
            return self.alpaca_paper
        return self._host_looks_like_paper(self.alpaca_base_url)

    def entry_window(self, session_close: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
        """(submit_at, hard_cutoff) for the closing-auction order."""
        return (
            session_close - dt.timedelta(minutes=self.entry_submit_offset_min),
            session_close - dt.timedelta(minutes=self.entry_cutoff_offset_min),
        )

    def exit_window(self, session_open: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
        """(submit_at, hard_cutoff) for the opening-auction order."""
        return (
            session_open - dt.timedelta(minutes=self.exit_submit_offset_min),
            session_open - dt.timedelta(minutes=self.exit_cutoff_offset_min),
        )

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.log_dir, self.cache_dir, self.reports_dir):
            Path(d).mkdir(parents=True, exist_ok=True)

    def redacted(self) -> dict:
        d = self.model_dump()
        for k in ("alpaca_key_id", "alpaca_secret_key", "notify_webhook"):
            if d.get(k):
                d[k] = f"<set:{len(str(d[k]))} chars>"
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in d.items()}


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)
