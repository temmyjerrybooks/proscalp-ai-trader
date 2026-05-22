from __future__ import annotations

from app.scoring.setup_score import SetupScoreInput, SetupScoringEngine


def test_setup_scoring_grades_aplus():
    result = SetupScoringEngine().score(
        SetupScoreInput(
            market_regime_score=100,
            session_timing_score=100,
            coin_strength_score=95,
            volume_confirmation_score=90,
            trend_alignment_score=90,
            liquidity_orderbook_score=90,
            risk_reward_score=90,
            btc_eth_confirmation_score=100,
            spread_slippage_score=95,
        )
    )

    assert result.total >= 90
    assert result.grade == "A+"
    assert result.permission == "aggressive entry allowed"


def test_setup_scoring_rejects_below_threshold():
    result = SetupScoringEngine().score(
        SetupScoreInput(30, 30, 30, 30, 30, 30, 30, 30, 30)
    )

    assert result.grade == "NO_TRADE"
    assert "setup score below C threshold" in result.rejection_reasons


def test_setup_scoring_grades_c_for_lower_quality_trade():
    result = SetupScoringEngine().score(
        SetupScoreInput(58, 58, 58, 58, 58, 58, 58, 58, 58)
    )

    assert result.grade == "C"
    assert result.permission == "tiny entry"
