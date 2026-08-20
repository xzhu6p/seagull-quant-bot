"""
现货策略集（与运行环境解耦：同一份代码用于实盘与回测）。

策略接口约定：
    on_bar(df, position, price) -> list[OrderIntent]
    - df: 按时间升序的K线 DataFrame（open/high/low/close/vol），
      最后一行为**已收盘**的最新一根K线
    - position: PositionState（只读视图）
    - price: 最新价格（df.close.iloc[-1]）

主交付为永续合约版 Seagull 策略（seagull.py），本模块为附加的
现货策略库（双均线 / RSI / 网格），可单独用于现货网格等场景。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

import pandas as pd

from .indicators import ema, rsi, sma
from .trader import OrderIntent

logger = logging.getLogger(__name__)


class BaseStrategy:
    """策略基类。子类实现 on_bar，返回订单意图列表。"""

    name: str = "base"

    def __init__(self, params: Dict[str, Any]) -> None:
        self.params = params

    def on_bar(self, df: pd.DataFrame, position, price: float) -> List[OrderIntent]:
        raise NotImplementedError

    def warmup_bars(self) -> int:
        """策略需要的最少K线数量。"""
        return 50


class MaCrossStrategy(BaseStrategy):
    """双均线交叉（趋势跟随）：快线上穿慢线买入，下穿卖出。"""

    name = "ma_cross"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.fast = int(params.get("fast", 20))
        self.slow = int(params.get("slow", 60))
        self.order_quote = float(params.get("order_quote", 100.0))
        self.ma_type = str(params.get("ma_type", "sma"))
        if self.fast >= self.slow:
            raise ValueError("fast 必须小于 slow")

    def warmup_bars(self) -> int:
        return self.slow + 5

    def _ma(self, s: pd.Series, n: int) -> pd.Series:
        return ema(s, n) if self.ma_type == "ema" else sma(s, n)

    def on_bar(self, df: pd.DataFrame, position, price: float) -> List[OrderIntent]:
        if len(df) < self.slow + 2:
            return []
        fast = self._ma(df["close"], self.fast)
        slow = self._ma(df["close"], self.slow)
        f_now, f_prev = fast.iloc[-1], fast.iloc[-2]
        s_now, s_prev = slow.iloc[-1], slow.iloc[-2]

        golden = f_now > s_now and f_prev <= s_prev   # 金叉（上穿）
        death = f_now < s_now and f_prev >= s_prev    # 死叉（下穿）

        if golden and position.base_qty <= 0:
            logger.info("[策略] MA金叉: fast=%.2f slow=%.2f → 买入", f_now, s_now)
            return [OrderIntent(side="buy", quote_amount=self.order_quote, reason="MA金叉")]
        if death and position.base_qty > 0:
            logger.info("[策略] MA死叉: fast=%.2f slow=%.2f → 卖出", f_now, s_now)
            return [OrderIntent(side="sell", base_amount=position.base_qty, reason="MA死叉")]
        return []


class RsiReversalStrategy(BaseStrategy):
    """RSI 超卖回升买入 / 超买回落卖出。"""

    name = "rsi_reversal"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.period = int(params.get("period", 14))
        self.oversold = float(params.get("oversold", 30))
        self.overbought = float(params.get("overbought", 70))
        self.order_quote = float(params.get("order_quote", 100.0))

    def warmup_bars(self) -> int:
        return self.period + 10

    def on_bar(self, df: pd.DataFrame, position, price: float) -> List[OrderIntent]:
        if len(df) < self.period + 2:
            return []
        r = rsi(df["close"], self.period)
        r_now, r_prev = r.iloc[-1], r.iloc[-2]
        if pd.isna(r_now) or pd.isna(r_prev):
            return []

        if r_prev < self.oversold <= r_now and position.base_qty <= 0:
            logger.info("[策略] RSI 超卖回升 %.1f → 买入", r_now)
            return [OrderIntent(side="buy", quote_amount=self.order_quote, reason="RSI超卖回升")]
        if r_prev > self.overbought >= r_now and position.base_qty > 0:
            logger.info("[策略] RSI 超买回落 %.1f → 卖出", r_now)
            return [OrderIntent(side="sell", base_amount=position.base_qty, reason="RSI超买回落")]
        return []


@dataclass
class GridState:
    """网格运行状态（可序列化持久化）。"""
    initialized: bool = False
    upper: float = 0.0
    lower: float = 0.0
    grids: int = 0
    step_quote: float = 0.0          # 每格投入金额
    last_level: int = -1             # 价格当前所在格（0 = 最底格）


class GridStrategy(BaseStrategy):
    """现货网格：价格每跌一格买入一格，每涨一格卖出一格；破上沿清仓。"""

    name = "grid"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.upper = float(params.get("upper", 0))
        self.lower = float(params.get("lower", 0))
        self.range_pct = float(params.get("range_pct", 0.20))
        self.grids = int(params.get("grids", 10))
        self.total_quote = float(params.get("total_quote", 500.0))
        self.state = GridState()

    def warmup_bars(self) -> int:
        return 5

    def _ensure_init(self, price: float) -> None:
        if self.state.initialized:
            return
        s = self.state
        s.upper = self.upper if self.upper > 0 else price * (1 + self.range_pct)
        s.lower = self.lower if self.lower > 0 else price * (1 - self.range_pct)
        s.grids = max(self.grids, 2)
        s.step_quote = self.total_quote / s.grids
        s.last_level = self._level(price)
        s.initialized = True
        logger.info(
            "[网格] 初始化: 区间 [%.2f, %.2f] %d 格, 每格 %.2f USDT, 当前第 %d 格",
            s.lower, s.upper, s.grids, s.step_quote, s.last_level,
        )

    def _level(self, price: float) -> int:
        s = self.state
        grid_width = (s.upper - s.lower) / s.grids
        if grid_width <= 0:
            return 0
        lv = int((price - s.lower) / grid_width)
        return max(0, min(s.grids - 1, lv))

    def on_bar(self, df: pd.DataFrame, position, price: float) -> List[OrderIntent]:
        self._ensure_init(price)
        s = self.state

        # 突破区间上沿 → 清仓锁定利润（跌破下沿持有，由风控止损兜底）
        if price >= s.upper and position.base_qty > 0:
            logger.info("[网格] 价格 %.2f 突破上沿 %.2f → 清仓", price, s.upper)
            s.last_level = self._level(price)
            return [OrderIntent(side="sell", base_amount=position.base_qty,
                                reason="网格破上沿清仓")]

        new_level = self._level(price)
        if new_level == s.last_level:
            return []

        intents: List[OrderIntent] = []
        if new_level < s.last_level:
            k = s.last_level - new_level                      # 下跌 k 格 → 买入 k 份
            amount = k * s.step_quote
            logger.info("[网格] 价格跌至第 %d 格（-%d）→ 买入 %.2f USDT", new_level, k, amount)
            intents.append(OrderIntent(side="buy", quote_amount=amount,
                                       reason=f"网格下移至第{new_level}格"))
        else:
            k = new_level - s.last_level                      # 上涨 k 格 → 卖出 k 份
            sell_qty = k * s.step_quote / price if price > 0 else 0.0
            if position.base_qty > 0:
                sell_qty = min(sell_qty, position.base_qty)
                logger.info("[网格] 价格涨至第 %d 格（+%d）→ 卖出 %.8f", new_level, k, sell_qty)
                intents.append(OrderIntent(side="sell", base_amount=sell_qty,
                                           reason=f"网格上移至第{new_level}格"))
        s.last_level = new_level
        return intents

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.state.__dict__)

    def load_state(self, d: Dict[str, Any]) -> None:
        if d:
            self.state = GridState(**d)
