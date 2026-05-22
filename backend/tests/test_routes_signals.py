from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.routes_signals import (
    _apply_signal_filters,
    _parse_signal_bound,
    _regime_at_signal,
    _signal_csv_row,
    _bot_decision_accepted,
    _decision_matches,
    _execution_status,
    _matching_risk_reasons,
    _normalize_setup,
    _normalize_symbol,
    _signal_page,
    _signal_report_filename,
    _setup_matches,
    _symbol_matches,
)
from app.config.settings import Settings
from app.database.models import MarketRegime, RiskEvent, Signal, Trade


def _signal(signal_id: str = "signal-1") -> Signal:
    row = Signal(
        id=signal_id,
        symbol="BTCUSDT",
        setup_name="EMA pullback scalp",
        direction="long",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit={"levels": [102.0]},
        confidence_score=80,
        accepted=True,
        reasons_for_entry=["trend aligned"],
        rejection_reasons=[],
    )
    row.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    return row


def test_matching_risk_reasons_prefers_exact_signal_id() -> None:
    signal = _signal("signal-1")
    wrong_signal_event = RiskEvent(
        severity="info",
        event_type="trade_rejected",
        message="wrong signal rejection",
        payload={"signal_id": "signal-2", "symbol": "BTCUSDT", "setup": "EMA pullback scalp"},
    )
    wrong_signal_event.created_at = signal.created_at
    exact_event = RiskEvent(
        severity="info",
        event_type="trade_rejected",
        message="exact signal rejection",
        payload={"signal_id": "signal-1", "symbol": "BTCUSDT", "setup": "EMA pullback scalp"},
    )
    exact_event.created_at = signal.created_at + timedelta(seconds=20)

    assert _matching_risk_reasons(signal, [wrong_signal_event, exact_event]) == ["exact signal rejection"]


def test_matching_risk_reasons_keeps_legacy_symbol_time_fallback() -> None:
    signal = _signal()
    legacy_event = RiskEvent(
        severity="info",
        event_type="setup_rejected",
        message="legacy rejection",
        payload={"symbol": "BTCUSDT", "setup": "EMA pullback scalp"},
    )
    legacy_event.created_at = signal.created_at + timedelta(seconds=20)

    assert _matching_risk_reasons(signal, [legacy_event]) == ["legacy rejection"]


def test_signal_filter_normalizers() -> None:
    assert _normalize_symbol("btc/usdt") == "BTCUSDT"
    assert _normalize_symbol(" eth-usdt ") == "ETHUSDT"
    assert _normalize_setup("  EMA   pullback  ") == "EMA pullback"


def test_decision_filter_matching() -> None:
    accepted = {"decision": "accepted"}
    rejected = {"decision": "rejected"}

    assert _decision_matches(accepted, "all") is True
    assert _decision_matches(accepted, "accepted") is True
    assert _decision_matches(accepted, "rejected") is False
    assert _decision_matches(rejected, "rejected") is True


def test_execution_status_separates_strategy_and_trade_outcomes() -> None:
    settings = Settings()
    signal = _signal("signal-1")
    trade = Trade(
        id="trade-1",
        symbol="BTCUSDT",
        side="long",
        exchange="binance",
        mode="testnet",
        setup_name="EMA pullback scalp",
        entry_price=100,
        stop_loss=99,
        take_profit={"levels": [102]},
        quantity=1,
        status="open",
    )

    assert _execution_status(signal, None, [], trade, settings) == "open"
    assert _execution_status(signal, None, ["session exposure cap exceeded"], None, settings) == "blocked"
    assert _execution_status(signal, None, [], None, settings) == "not_selected"
    signal.accepted = False
    assert _execution_status(signal, None, [], None, settings) == "strategy_rejected"


def test_bot_decision_accepts_only_real_trade_statuses() -> None:
    assert _bot_decision_accepted("open") is True
    assert _bot_decision_accepted("closed") is True
    assert _bot_decision_accepted("not_selected") is False
    assert _bot_decision_accepted("blocked") is False


def test_symbol_and_setup_filter_matching() -> None:
    row = {"symbol": "BTCUSDT", "setup_type": "EMA pullback scalp"}

    assert _symbol_matches(row, "BTCUSDT") is True
    assert _symbol_matches(row, "ETHUSDT") is False
    assert _setup_matches(row, "ema") is True
    assert _setup_matches(row, "VWAP") is False


def test_apply_signal_filters_combines_decision_symbol_and_setup() -> None:
    rows = [
        {"symbol": "BTCUSDT", "setup_type": "EMA pullback scalp", "decision": "accepted"},
        {"symbol": "BTCUSDT", "setup_type": "VWAP reclaim scalp", "decision": "rejected"},
        {"symbol": "ETHUSDT", "setup_type": "EMA pullback scalp", "decision": "accepted"},
    ]

    assert _apply_signal_filters(rows, "accepted", "BTCUSDT", "EMA") == [rows[0]]
    assert _apply_signal_filters(rows, "rejected", None, "VWAP") == [rows[1]]
    assert _apply_signal_filters(rows, "all", "ETHUSDT", None) == [rows[2]]


def test_apply_signal_filters_supports_status_verdict_side_and_score() -> None:
    rows = [
        {
            "symbol": "BTCUSDT",
            "setup_type": "EMA pullback scalp",
            "direction": "long",
            "decision": "accepted",
            "setup_score": 84,
            "execution_status": "open",
            "follow_up": {"status": "actual_open_positive", "verdict": "pending", "settled": False},
        },
        {
            "symbol": "ETHUSDT",
            "setup_type": "VWAP reclaim scalp",
            "direction": "short",
            "decision": "rejected",
            "setup_score": 62,
            "execution_status": "strategy_rejected",
            "follow_up": {"status": "would_lose", "verdict": "good_rejection", "settled": True},
        },
    ]

    assert _apply_signal_filters(
        rows,
        "accepted",
        None,
        None,
        side="long",
        execution_status="open",
        follow_up_status="actual open positive",
        verdict="pending",
        min_score=80,
        max_score=90,
    ) == [rows[0]]
    assert _apply_signal_filters(rows, "all", None, None, verdict="good_rejection") == [rows[1]]
    assert _apply_signal_filters(rows, "all", None, None, settled_only=True) == [rows[1]]


def test_parse_signal_bound_includes_full_end_date() -> None:
    assert _parse_signal_bound("2026-05-18", end=False) == datetime(2026, 5, 17, 23, tzinfo=timezone.utc)
    assert _parse_signal_bound("2026-05-18", end=True) == datetime(2026, 5, 18, 23, tzinfo=timezone.utc)


def test_regime_at_signal_uses_latest_regime_before_signal() -> None:
    signal = _signal()
    old_regime = MarketRegime(regime="good", tradable=True, score=70)
    old_regime.created_at = signal.created_at - timedelta(minutes=5)
    current_regime = MarketRegime(regime="strong", tradable=True, score=82)
    current_regime.created_at = signal.created_at - timedelta(seconds=20)

    assert _regime_at_signal(signal, [old_regime, current_regime]) == "strong"


def test_signal_csv_row_flattens_followup_and_reasons() -> None:
    row = _signal_csv_row(
        {
            "created_at": datetime(2026, 5, 18, 8, 15, tzinfo=timezone.utc),
            "symbol": "BTCUSDT",
            "setup_type": "EMA pullback scalp",
            "direction": "long",
            "setup_score": 84,
            "grade": "A",
            "decision": "accepted",
            "strategy_status": "accepted",
            "execution_status": "open",
            "signal_session": "london",
            "signal_regime": "strong",
            "reason_summary": "trend aligned",
            "reason_for_entry": ["trend aligned"],
            "rejection_reasons": [],
            "follow_up": {
                "status": "actual_positive",
                "verdict": "good_acceptance",
                "pnl_pct": 0.2,
                "settled": True,
            },
        }
    )

    assert row["created_at"] == "2026-05-18T08:15:00+00:00"
    assert row["follow_up_status"] == "actual_positive"
    assert row["follow_up_settled"] is True
    assert row["decision_quality"] == "good_acceptance"
    assert row["signal_session"] == "london"
    assert row["signal_regime"] == "strong"
    assert row["entry_reasons"] == "trend aligned"


def test_signal_page_metadata() -> None:
    page = _signal_page(
        items=[{"symbol": "BTCUSDT"}],
        total=75,
        limit=50,
        offset=0,
        source_window=250,
        filters={"decision": "all"},
    )

    assert page["items"] == [{"symbol": "BTCUSDT"}]
    assert page["has_next"] is True
    assert page["source_window"] == 250


def test_signal_report_filename_uses_full_requested_period() -> None:
    assert (
        _signal_report_filename("all", "2026-05-16", "2026-05-18")
        == "proscalp_signals_all_2026-05-16_to_2026-05-18.csv"
    )
