from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.exchanges.base import ExchangeAdapter
from app.paper_trading.simulator import PaperTradingSimulator


@dataclass(slots=True)
class ManagedPosition:
    trade_id: str
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit_levels: list[float]
    quantity: float
    opened_at: datetime
    break_even_moved: bool = False


class PositionManager:
    def __init__(self, adapter: ExchangeAdapter, paper: PaperTradingSimulator) -> None:
        self.adapter = adapter
        self.paper = paper
        self.managed: dict[str, ManagedPosition] = {}

    def register(self, position: ManagedPosition) -> None:
        self.managed[position.trade_id] = position

    def move_to_break_even_if_ready(self, trade_id: str, price: float, trigger_r: float = 0.8) -> bool:
        position = self.managed[trade_id]
        risk = abs(position.entry_price - position.stop_loss)
        if risk <= 0 or position.break_even_moved:
            return False
        profit = (price - position.entry_price) if position.side == "long" else (position.entry_price - price)
        if profit >= risk * trigger_r:
            position.stop_loss = position.entry_price
            position.break_even_moved = True
            return True
        return False

    def time_exit_candidates(self, max_minutes: int) -> list[ManagedPosition]:
        now = datetime.now(timezone.utc)
        return [
            position
            for position in self.managed.values()
            if (now - position.opened_at).total_seconds() / 60 >= max_minutes
        ]

    async def emergency_close_all(self) -> dict[str, int]:
        live_positions = await self.adapter.fetch_positions()
        closed = 0
        for position in live_positions:
            await self.adapter.close_position(position.symbol)
            closed += 1
        paper_closed = len(self.paper.close_all({}, reason="emergency_close"))
        return {"live_closed": closed, "paper_closed": paper_closed}
