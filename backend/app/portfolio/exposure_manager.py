from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import Settings, get_settings


@dataclass(slots=True)
class ExposurePosition:
    symbol: str
    side: str
    notional: float
    session: str
    open_risk: float = 0.0
    source: str = "database"
    beta_group: str = "major"


@dataclass(slots=True)
class ExposureDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    diagnostics: dict[str, float | int | str] = field(default_factory=dict)


class ExposureManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def can_open(
        self,
        account_equity: float,
        candidate_symbol: str,
        candidate_side: str,
        candidate_notional: float,
        candidate_open_risk: float,
        session: str,
        market_regime: str,
        open_positions: list[ExposurePosition],
        btc_eth_confirmation: bool,
    ) -> ExposureDecision:
        reasons: list[str] = []
        if len(open_positions) >= self.settings.max_concurrent_trades:
            reasons.append("max concurrent trades reached")
        same_coin = [position for position in open_positions if position.symbol == candidate_symbol]
        if len(same_coin) >= self.settings.max_trades_per_coin:
            reasons.append("active trade already exists for coin")
        total_notional = sum(position.notional for position in open_positions) + candidate_notional
        coin_notional = sum(position.notional for position in same_coin) + candidate_notional
        session_notional = sum(position.notional for position in open_positions if position.session == session) + candidate_notional
        total_open_risk = sum(position.open_risk for position in open_positions) + candidate_open_risk
        session_open_risk = (
            sum(position.open_risk for position in open_positions if position.session == session) + candidate_open_risk
        )
        total_cap = self._total_notional_cap(market_regime)
        session_cap = self._session_notional_cap(session, market_regime)
        open_risk_cap = self._open_risk_cap(session, market_regime)
        diagnostics = self._diagnostics(
            account_equity=account_equity,
            current_total_notional=sum(position.notional for position in open_positions),
            current_session_notional=sum(position.notional for position in open_positions if position.session == session),
            candidate_notional=candidate_notional,
            total_notional=total_notional,
            coin_notional=coin_notional,
            session_notional=session_notional,
            current_open_risk=sum(position.open_risk for position in open_positions),
            current_session_open_risk=sum(position.open_risk for position in open_positions if position.session == session),
            candidate_open_risk=candidate_open_risk,
            total_open_risk=total_open_risk,
            session_open_risk=session_open_risk,
            total_cap=total_cap,
            coin_cap=self.settings.max_coin_exposure_pct,
            session_cap=session_cap,
            open_risk_cap=open_risk_cap,
            session=session,
            market_regime=market_regime,
            open_positions=len(open_positions),
        )
        if account_equity > 0:
            if diagnostics["total_notional_pct"] > total_cap:
                reasons.append(self._cap_reason("total exposure cap exceeded", diagnostics["total_notional_pct"], total_cap))
            if diagnostics["coin_notional_pct"] > self.settings.max_coin_exposure_pct:
                reasons.append(
                    self._cap_reason(
                        "per-coin exposure cap exceeded",
                        diagnostics["coin_notional_pct"],
                        self.settings.max_coin_exposure_pct,
                    )
                )
            if diagnostics["session_notional_pct"] > session_cap:
                reasons.append(self._cap_reason("session exposure cap exceeded", diagnostics["session_notional_pct"], session_cap))
            if diagnostics["total_open_risk_pct"] > open_risk_cap:
                reasons.append(self._cap_reason("open risk cap exceeded", diagnostics["total_open_risk_pct"], open_risk_cap))
        same_direction = [position for position in open_positions if position.side == candidate_side]
        if len(same_direction) >= 4 and not btc_eth_confirmation:
            reasons.append("too many correlated same-direction trades without BTC/ETH confirmation")
        return ExposureDecision(allowed=not reasons, reasons=reasons or ["exposure limits passed"], diagnostics=diagnostics)

    def _total_notional_cap(self, market_regime: str) -> float:
        regime = market_regime.lower()
        if regime == "hot":
            return max(self.settings.max_total_exposure_pct, self.settings.max_total_exposure_hot_pct)
        if regime == "strong":
            return max(self.settings.max_total_exposure_pct, self.settings.max_total_exposure_strong_pct)
        return self.settings.max_total_exposure_pct

    def _session_notional_cap(self, session: str, market_regime: str) -> float:
        if session == "off_session":
            return self.settings.max_session_exposure_off_session_pct
        regime = market_regime.lower()
        if regime == "hot":
            return self.settings.max_session_exposure_hot_pct
        if regime == "strong":
            return self.settings.max_session_exposure_strong_pct
        if regime in {"good", "unclear"}:
            return self.settings.max_session_exposure_normal_pct
        return self.settings.max_session_exposure_pct

    def _open_risk_cap(self, session: str, market_regime: str) -> float:
        if session == "off_session":
            return self.settings.max_open_risk_off_session_pct
        regime = market_regime.lower()
        if regime == "hot":
            return self.settings.max_open_risk_hot_pct
        if regime == "strong":
            return self.settings.max_open_risk_strong_pct
        return self.settings.max_open_risk_pct

    @staticmethod
    def _cap_reason(label: str, value: float | int | str, cap: float) -> str:
        return f"{label}: {float(value):.2f}% > {cap:.2f}%"

    @staticmethod
    def _diagnostics(
        account_equity: float,
        current_total_notional: float,
        current_session_notional: float,
        candidate_notional: float,
        total_notional: float,
        coin_notional: float,
        session_notional: float,
        current_open_risk: float,
        current_session_open_risk: float,
        candidate_open_risk: float,
        total_open_risk: float,
        session_open_risk: float,
        total_cap: float,
        coin_cap: float,
        session_cap: float,
        open_risk_cap: float,
        session: str,
        market_regime: str,
        open_positions: int,
    ) -> dict[str, float | int | str]:
        base = max(account_equity, 1e-12)
        return {
            "account_equity": round(account_equity, 4),
            "current_total_notional": round(current_total_notional, 4),
            "current_session_notional": round(current_session_notional, 4),
            "candidate_notional": round(candidate_notional, 4),
            "total_notional": round(total_notional, 4),
            "coin_notional": round(coin_notional, 4),
            "session_notional": round(session_notional, 4),
            "current_open_risk": round(current_open_risk, 4),
            "current_session_open_risk": round(current_session_open_risk, 4),
            "candidate_open_risk": round(candidate_open_risk, 4),
            "total_open_risk": round(total_open_risk, 4),
            "session_open_risk": round(session_open_risk, 4),
            "current_total_notional_pct": round(current_total_notional / base * 100, 4),
            "current_session_notional_pct": round(current_session_notional / base * 100, 4),
            "candidate_notional_pct": round(candidate_notional / base * 100, 4),
            "total_notional_pct": round(total_notional / base * 100, 4),
            "coin_notional_pct": round(coin_notional / base * 100, 4),
            "session_notional_pct": round(session_notional / base * 100, 4),
            "current_open_risk_pct": round(current_open_risk / base * 100, 4),
            "current_session_open_risk_pct": round(current_session_open_risk / base * 100, 4),
            "candidate_open_risk_pct": round(candidate_open_risk / base * 100, 4),
            "total_open_risk_pct": round(total_open_risk / base * 100, 4),
            "session_open_risk_pct": round(session_open_risk / base * 100, 4),
            "total_cap_pct": total_cap,
            "coin_cap_pct": coin_cap,
            "session_cap_pct": session_cap,
            "open_risk_cap_pct": open_risk_cap,
            "session": session,
            "market_regime": market_regime,
            "open_positions": open_positions,
        }
