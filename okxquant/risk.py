"""
风控管理器：所有订单必须通过这里的安全检查才会被执行。

规则（均可在 config.json 配置）：
- max_order_quote      单笔订单最大金额（计价币）
- max_position_quote   最大持仓名义价值（计价币）
- stop_loss_pct        止损比例（价格跌破 avg_cost * (1-pct) 强制平仓）
- take_profit_pct      止盈比例
- daily_max_loss       单日最大亏损（已实现+浮动），触发后当日停止交易
- cooldown_seconds     两笔订单的最小时间间隔
- price_sanity_pct     最新价偏离近 N 根均价的上限（防插针/坏数据），超出则拒单
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    max_order_quote: float = 100.0
    max_position_quote: float = 500.0
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.0      # 0 表示不启用
    daily_max_loss: float = 50.0
    cooldown_seconds: int = 60
    price_sanity_pct: float = 0.10


class RiskManager:
    def __init__(self, cfg: RiskConfig, position) -> None:
        self.cfg = cfg
        self.position = position
        self._last_order_time: float = 0.0
        self._daily_loss: float = 0.0
        self._daily_date: Optional[date] = None
        self._halted_today: bool = False

    # ------------------------------------------------------------------
    def on_new_day(self) -> None:
        """跨日重置当日亏损与熔断标志（引擎每天调用一次）。"""
        today = date.today()
        if self._daily_date != today:
            self._daily_date = today
            self._daily_loss = 0.0
            self._halted_today = False

    def register_realized_change(self, delta_pnl: float, price: float) -> None:
        """当日已实现盈亏变动 + 当前浮亏 → 判断是否熔断。"""
        if delta_pnl:
            self._daily_loss -= delta_pnl
        unreal = self.position.state.unrealized_pnl(price)
        total_loss = self._daily_loss - unreal
        if self.cfg.daily_max_loss > 0 and total_loss >= self.cfg.daily_max_loss:
            if not self._halted_today:
                logger.error(
                    "[风控] 当日亏损 %.2f 已达上限 %.2f，今日停止交易！",
                    total_loss, self.cfg.daily_max_loss,
                )
            self._halted_today = True

    @property
    def halted(self) -> bool:
        return self._halted_today

    # ------------------------------------------------------------------
    def check_order(
        self,
        side: str,
        quote_amount: float,
        price: float,
        df_recent: pd.DataFrame,
    ) -> tuple[bool, str]:
        """下单前检查，返回 (是否通过, 原因)。"""
        now_ok, reason = self._cooldown_check()
        if not now_ok:
            return False, reason
        if self._halted_today:
            return False, "当日亏损已达上限，交易已熔断"

        if quote_amount <= 0:
            return False, "订单金额必须大于 0"
        if quote_amount > self.cfg.max_order_quote:
            return False, f"单笔金额 {quote_amount:.2f} 超过上限 {self.cfg.max_order_quote}"

        # 价格合理性：偏离最近均值过大则拒单（防插针/脏数据）
        if df_recent is not None and len(df_recent) >= 10:
            ref = float(df_recent["close"].tail(30).mean())
            if ref > 0:
                dev = abs(price - ref) / ref
                if dev > self.cfg.price_sanity_pct:
                    return False, (
                        f"价格 {price} 偏离近30根均值 {ref:.2f} 达 {dev:.1%}，"
                        f"超过 {self.cfg.price_sanity_pct:.0%}，疑似异常行情，拒单"
                    )

        if side == "buy":
            # 持仓名义价值上限
            current_value = self.position.state.base_qty * price
            if current_value + quote_amount > self.cfg.max_position_quote:
                return False, (
                    f"买入后持仓 {current_value + quote_amount:.2f} 将超过上限 "
                    f"{self.cfg.max_position_quote}"
                )
        return True, "ok"

    def check_stop(self, price: float) -> Optional[str]:
        """止损/止盈检查，返回 'sell' 表示应强制平仓，否则 None。"""
        s = self.position.state
        if s.base_qty <= 0 or s.avg_cost <= 0:
            return None
        if self.cfg.stop_loss_pct > 0:
            if price <= s.avg_cost * (1 - self.cfg.stop_loss_pct):
                logger.warning(
                    "[风控] 触发止损: 现价 %.2f <= 均价 %.2f × (1-%.0f%%)",
                    price, s.avg_cost, self.cfg.stop_loss_pct * 100,
                )
                return "sell"
        if self.cfg.take_profit_pct > 0:
            if price >= s.avg_cost * (1 + self.cfg.take_profit_pct):
                logger.info("[风控] 触发止盈: 现价 %.2f >= 均价 %.2f × (1+%.0f%%)",
                            price, s.avg_cost, self.cfg.take_profit_pct * 100)
                return "sell"
        return None

    # ------------------------------------------------------------------
    def mark_order_time(self) -> None:
        import time
        self._last_order_time = time.time()

    def _cooldown_check(self) -> tuple[bool, str]:
        import time
        elapsed = time.time() - self._last_order_time
        if elapsed < self.cfg.cooldown_seconds:
            return False, f"距上一笔订单仅 {elapsed:.0f}s < 冷却 {self.cfg.cooldown_seconds}s"
        return True, "ok"
