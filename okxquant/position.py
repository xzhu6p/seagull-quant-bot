"""
持仓跟踪器：维护基础币持仓数量、持仓均价、已实现盈亏，并持久化到 state.json。

- on_fill(): 每笔成交后更新（含手续费，计价币单位）
- 用于风控（止损基于 avg_cost）与状态恢复（进程重启不丢仓位）
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    base_qty: float = 0.0        # 持有基础币数量（如 BTC）
    quote_qty: float = 0.0       # 持仓占用计价币成本（如 USDT）
    avg_cost: float = 0.0        # 持仓均价
    realized_pnl: float = 0.0    # 累计已实现盈亏（扣手续费前）
    total_fees: float = 0.0      # 累计手续费（计价币）
    last_price: float = 0.0      # 最近一次成交价

    def unrealized_pnl(self, price: float) -> float:
        if self.base_qty <= 0 or self.avg_cost <= 0:
            return 0.0
        return (price - self.avg_cost) * self.base_qty


class PositionTracker:
    """持仓状态机 + JSON 持久化。"""

    def __init__(self, state_file: str) -> None:
        self.state_file = state_file
        self.state = PositionState()
        self._load()

    # ------------------------------------------------------------------
    def on_fill(self, side: str, base_qty: float, price: float, fee_quote: float = 0.0) -> None:
        """按成交回报更新持仓。

        买入：增加数量与成本；卖出：减少数量，实现盈亏 = (价-均价)*量。
        fee_quote: 该笔手续费（折算为计价币，OKX 现货手续费以收到币计收，
        这里统一近似折算为 quote）。
        """
        s = self.state
        if side == "buy":
            new_qty = s.base_qty + base_qty
            s.quote_qty += base_qty * price
            s.avg_cost = s.quote_qty / new_qty if new_qty > 0 else 0.0
            s.base_qty = new_qty
        elif side == "sell":
            sell_qty = min(base_qty, s.base_qty)
            s.realized_pnl += (price - s.avg_cost) * sell_qty
            s.quote_qty -= sell_qty * s.avg_cost
            s.base_qty -= sell_qty
            if s.base_qty <= 1e-12:
                s.base_qty = 0.0
                s.quote_qty = 0.0
                s.avg_cost = 0.0
        s.total_fees += fee_quote
        s.last_price = price
        self._save()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.state = PositionState(**data.get("position", {}))
                logger.info(
                    "已恢复持仓状态: qty=%.8f avg_cost=%.2f realized_pnl=%.2f",
                    self.state.base_qty, self.state.avg_cost, self.state.realized_pnl,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("读取持仓状态失败(%s)，从零开始", e)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({"position": asdict(self.state)}, f, indent=2, ensure_ascii=False)
