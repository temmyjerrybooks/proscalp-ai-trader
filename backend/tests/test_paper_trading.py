from __future__ import annotations

from app.paper_trading.simulator import PaperTradingSimulator


def test_paper_trading_simulates_entries_exits_and_partial_fills():
    simulator = PaperTradingSimulator(starting_equity=10_000, fee_bps=6, slippage_bps=2)
    position = simulator.open_position("ETHUSDT", "long", 1, 100, 99, [101, 102, 103])
    fills = simulator.update_price("ETHUSDT", 101.5)

    assert position.id in simulator.positions
    assert any(fill.reason == "tp1" for fill in fills)
    assert simulator.positions[position.id].remaining_quantity < 1

    final = simulator.close_position(position.id, 102, "manual")
    assert final.pnl > 0
    assert position.id not in simulator.positions
    assert simulator.max_drawdown >= 0
