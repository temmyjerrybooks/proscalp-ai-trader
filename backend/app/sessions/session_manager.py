from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config.settings import Settings, get_settings


@dataclass(slots=True)
class SessionState:
    name: str
    active: bool
    tradable: bool
    aggression_mode: bool
    start_utc: datetime
    end_utc: datetime
    user_time: datetime
    notes: list[str]


class SessionManager:
    """Tracks Asia, London, and New York scalp windows using UTC internally."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.user_timezone = ZoneInfo(self.settings.user_timezone)

    def current_sessions(self, now: datetime | None = None, regime: str = "unclear") -> list[SessionState]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        windows = {
            "asia": (self.settings.asia_session_start_utc, self.settings.asia_session_end_utc),
            "london": (self.settings.london_session_start_utc, self.settings.london_session_end_utc),
            "new_york": (self.settings.new_york_session_start_utc, self.settings.new_york_session_end_utc),
        }
        return [self._state_for(name, start, end, now, regime) for name, (start, end) in windows.items()]

    def active_session(self, now: datetime | None = None, regime: str = "unclear") -> SessionState:
        sessions = self.current_sessions(now=now, regime=regime)
        for session in sessions:
            if session.active:
                return session
        return sessions[0]

    def trading_session(self, now: datetime | None = None, regime: str = "unclear") -> SessionState:
        sessions = self.current_sessions(now=now, regime=regime)
        for session in sessions:
            if session.active:
                return session
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        tradable = self.settings.off_session_trading_enabled and regime.lower() not in {"dangerous", "bad"}
        notes = ["outside Asia/London/New York; stricter off-session score threshold applies"]
        if not self.settings.off_session_trading_enabled:
            notes.append("off-session trading disabled")
        return SessionState(
            name="off_session",
            active=False,
            tradable=tradable,
            aggression_mode=False,
            start_utc=now,
            end_utc=now,
            user_time=now.astimezone(self.user_timezone),
            notes=notes,
        )

    def asian_range(self, candles: list) -> tuple[float, float]:
        if not candles:
            return 0.0, 0.0
        return max(candle.high for candle in candles), min(candle.low for candle in candles)

    def _state_for(
        self,
        name: str,
        start_value: str,
        end_value: str,
        now: datetime,
        regime: str,
    ) -> SessionState:
        start_time = self._parse_time(start_value)
        end_time = self._parse_time(end_value)
        start = datetime.combine(now.date(), start_time, tzinfo=timezone.utc)
        end = datetime.combine(now.date(), end_time, tzinfo=timezone.utc)
        if end <= start:
            end += timedelta(days=1)
        if now < start and end.date() > start.date():
            start -= timedelta(days=1)
            end -= timedelta(days=1)

        active = start <= now <= end
        open_window = start <= now <= start + timedelta(minutes=90)
        strong_market = regime.lower() in {"strong", "hot"}
        aggression = active and open_window and strong_market and self.settings.aggression_mode_enabled
        notes = []
        if aggression:
            notes.append("session aggression mode active")
        if name == "london":
            notes.append("watch Asian range breakout, fakeout, VWAP reclaim, EMA pullback")
        elif name == "new_york":
            notes.append("watch London continuation/reversal and US volatility expansion")
        else:
            notes.append("watch Tokyo/Hong Kong liquidity and early momentum")

        return SessionState(
            name=name,
            active=active,
            tradable=active and regime.lower() not in {"dangerous", "bad"},
            aggression_mode=aggression,
            start_utc=start,
            end_utc=end,
            user_time=now.astimezone(self.user_timezone),
            notes=notes,
        )

    @staticmethod
    def _parse_time(value: str) -> time:
        hour, minute = value.split(":")
        return time(int(hour), int(minute), tzinfo=timezone.utc)
