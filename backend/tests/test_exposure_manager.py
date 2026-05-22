from __future__ import annotations

from app.config.settings import Settings
from app.portfolio.exposure_manager import ExposureManager, ExposurePosition


def _manager() -> ExposureManager:
    return ExposureManager(
        Settings(
            max_total_exposure_pct=35,
            max_total_exposure_strong_pct=45,
            max_total_exposure_hot_pct=50,
            max_coin_exposure_pct=10,
            max_session_exposure_off_session_pct=25,
            max_session_exposure_normal_pct=35,
            max_session_exposure_strong_pct=40,
            max_session_exposure_hot_pct=50,
            max_open_risk_pct=1.5,
            max_open_risk_off_session_pct=1.25,
            max_open_risk_strong_pct=2.0,
            max_open_risk_hot_pct=2.5,
        )
    )


def test_session_exposure_counts_only_positions_opened_in_same_session() -> None:
    decision = _manager().can_open(
        account_equity=10_000,
        candidate_symbol="ETHUSDT",
        candidate_side="long",
        candidate_notional=1_000,
        candidate_open_risk=40,
        session="off_session",
        market_regime="strong",
        open_positions=[
            ExposurePosition("BTCUSDT", "long", notional=2_000, session="london", open_risk=80),
        ],
        btc_eth_confirmation=True,
    )

    assert decision.allowed is True
    assert decision.diagnostics["current_session_notional_pct"] == 0
    assert decision.diagnostics["session_notional_pct"] == 10


def test_open_risk_cap_blocks_even_when_notional_is_acceptable() -> None:
    decision = _manager().can_open(
        account_equity=10_000,
        candidate_symbol="ETHUSDT",
        candidate_side="long",
        candidate_notional=500,
        candidate_open_risk=30,
        session="london",
        market_regime="good",
        open_positions=[
            ExposurePosition("BTCUSDT", "long", notional=500, session="london", open_risk=80),
            ExposurePosition("SOLUSDT", "long", notional=500, session="asia", open_risk=60),
        ],
        btc_eth_confirmation=True,
    )

    assert decision.allowed is False
    assert any("open risk cap exceeded" in reason for reason in decision.reasons)
    assert decision.diagnostics["total_open_risk_pct"] == 1.7


def test_hot_regime_unlocks_wider_notional_cap_without_disabling_risk_controls() -> None:
    manager = _manager()
    positions = [
        ExposurePosition("BTCUSDT", "long", notional=3_000, session="new_york", open_risk=50),
    ]

    good = manager.can_open(
        account_equity=10_000,
        candidate_symbol="ETHUSDT",
        candidate_side="long",
        candidate_notional=1_000,
        candidate_open_risk=30,
        session="new_york",
        market_regime="good",
        open_positions=positions,
        btc_eth_confirmation=True,
    )
    hot = manager.can_open(
        account_equity=10_000,
        candidate_symbol="ETHUSDT",
        candidate_side="long",
        candidate_notional=1_000,
        candidate_open_risk=30,
        session="new_york",
        market_regime="hot",
        open_positions=positions,
        btc_eth_confirmation=True,
    )

    assert good.allowed is False
    assert any("total exposure cap exceeded" in reason for reason in good.reasons)
    assert hot.allowed is True
    assert hot.diagnostics["session_cap_pct"] == 50
