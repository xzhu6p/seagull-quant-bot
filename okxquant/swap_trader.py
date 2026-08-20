"""
合约交易执行器：把 Seagull 信号变成真实的 OKX 永续合约订单。

执行链路：
  信号 → 张数换算(名义价值) → 市价开仓 → 按实际成交价挂 OCO(止盈+止损)
  → 每周期追踪止损(修改 SL 触发价) → 平仓检测(OCO 触发) → 连亏计数

设计要点：
- SL/TP 挂在交易所侧（OCO），机器人掉线/崩溃止损依然生效——保命优先
- 连续亏损 ≥ N 次 → 名义价值降至降仓档（对应 EA 手数 0.03 → 0.01）
- 所有状态持久化到 state.json，进程重启自动恢复
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from .base_client import SwapClientBase
from .exchange_error import ExchangeApiError
from .seagull import SeagullSignal, SeagullStrategy

logger = logging.getLogger(__name__)


@dataclass
class SwapPositionState:
    """本地仓位镜像（每 instId 一份）。"""
    direction: str = ""        # long / short
    contracts: int = 0
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    algo_id: str = ""
    ord_id: str = ""
    atr: float = 0.0
    opened_at: int = 0
    signal_reason: str = ""

    @property
    def is_open(self) -> bool:
        return bool(self.direction) and self.contracts > 0


class SwapTrader:
    """单个合约 instId 的执行器。"""

    def __init__(
        self,
        client: SwapClientBase,
        strategy: SeagullStrategy,
        inst: Dict[str, Any],
        td_mode: str = "isolated",
        state: Optional[SwapPositionState] = None,
    ) -> None:
        self.client = client
        self.strategy = strategy
        self.inst = inst
        self.td_mode = td_mode
        self.pos = state or SwapPositionState()
        self._last_seen_pos_id = ""   # 已处理的平仓记录 posId 去重

    # ==================================================================
    # 开仓
    # ==================================================================
    def open_position(self, signal: SeagullSignal) -> bool:
        """按信号市价开仓并挂 OCO 止盈止损。成功返回 True。"""
        ticker = self.client.get_ticker(self.inst["instId"])
        price = ticker["last"]

        notional = self.strategy.current_notional()
        contracts = self.client.contracts_for_notional(self.inst, notional, price)
        if contracts <= 0:
            # 名义价值太小（如降仓档）：用最小张数
            contracts = int(self.inst["minSz"])
            logger.info(
                "[执行] %s 名义价值 %.2f 不足一张，使用最小 %d 张",
                self.inst["instId"], notional, contracts,
            )

        side = "buy" if signal.direction == "long" else "sell"
        try:
            ord_id = self.client.market_order(
                self.inst["instId"], side, contracts, td_mode=self.td_mode
            )
        except ExchangeApiError as e:
            logger.error("[执行] %s 开仓被拒: %s %s", self.inst["instId"], e.code, e.msg)
            return False

        info = self.client.wait_order_filled(self.inst["instId"], ord_id)
        if info["state"] != "filled" or info["accFillSz"] <= 0:
            logger.error("[执行] 开仓未成交(state=%s)，撤销残留", info["state"])
            self.client.cancel_order(self.inst["instId"], ord_id)
            return False

        entry = info["avgPx"] or price
        filled = int(info["accFillSz"])  # 合约张数

        # 按实际成交价重算 SL/TP（对应 EA: SL=ask±1.5ATR / TP=ask±2ATR）
        p = self.strategy.p
        if signal.direction == "long":
            sl = self.client.round_px(self.inst, entry - p.sl_atr * signal.atr)
            tp = self.client.round_px(self.inst, entry + p.tp_atr * signal.atr, up=True)
            close_side = "sell"
        else:
            sl = self.client.round_px(self.inst, entry + p.sl_atr * signal.atr, up=True)
            tp = self.client.round_px(self.inst, entry - p.tp_atr * signal.atr)
            close_side = "buy"

        try:
            algo_id = self.client.place_oco(
                self.inst["instId"], close_side, filled, tp, sl, td_mode=self.td_mode
            )
        except ExchangeApiError as e:
            # OCO 失败必须立刻补救：直接市价平仓，绝不让仓位裸奔
            logger.critical(
                "[执行] %s OCO 止盈止损挂单失败(%s %s)！立即市价平仓保护资金",
                self.inst["instId"], e.code, e.msg,
            )
            self.client.market_order(
                self.inst["instId"], close_side, filled,
                td_mode=self.td_mode, reduce_only=True,
            )
            return False

        self.pos = SwapPositionState(
            direction=signal.direction,
            contracts=filled,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            algo_id=algo_id,
            ord_id=ord_id,
            atr=signal.atr,
            opened_at=int(time.time() * 1000),
            signal_reason=signal.reason,
        )
        logger.info(
            "[成交] %s %s %d 张 @ %.2f | SL=%.2f TP=%.2f | %s",
            self.inst["instId"],
            "开多" if signal.direction == "long" else "开空",
            filled, entry, sl, tp, signal.reason,
        )
        return True

    # ==================================================================
    # 持仓维护：追踪止损 / 平仓检测
    # ==================================================================
    def maintain(self) -> None:
        """每轮询周期调用：追踪止损 + 检测 OCO 是否已触发平仓。"""
        if not self.pos.is_open:
            return

        # 1) 仓位是否还在（被 OCO 平掉 / 强平则消失）
        live = self.client.get_position(self.inst["instId"])
        if live is None or abs(live["contracts"]) < self.pos.contracts:
            self._on_position_closed()
            return

        # 2) 微型追踪锁利
        if self.strategy.p.use_trailing:
            ticker = self.client.get_ticker(self.inst["instId"])
            new_sl = self.strategy.trailing_stop(
                self.pos.direction,
                self.pos.entry_price,
                ticker["last"],
                self.pos.sl_price,
                self.pos.atr,
            )
            if new_sl is not None:
                new_sl = self.client.round_px(
                    self.inst, new_sl, up=(self.pos.direction == "short")
                )
                if new_sl != self.pos.sl_price and self.pos.algo_id:
                    if self.client.amend_algo_sl(self.inst["instId"], self.pos.algo_id, new_sl):
                        logger.info(
                            "[追踪] %s 止损上移 %.2f → %.2f (entry=%.2f)",
                            self.inst["instId"], self.pos.sl_price, new_sl, self.pos.entry_price,
                        )
                        self.pos.sl_price = new_sl

    def _on_position_closed(self) -> None:
        """仓位已被平仓（OCO/强平）：统计盈亏、更新连亏计数、清理挂单。"""
        closed_pnl = 0.0
        try:
            history = self.client.get_positions_history(self.inst["instId"], limit=5)
            for h in history:
                if h["uTime"] >= self.pos.opened_at - 60_000:
                    closed_pnl = h["realizedPnl"]
                    break
        except Exception as e:  # noqa: BLE001
            logger.warning("[执行] 查询平仓历史失败(%s)，连亏统计可能不准", e)

        if closed_pnl < 0:
            self.strategy.losing_streak += 1
        elif closed_pnl > 0:
            self.strategy.losing_streak = 0

        logger.info(
            "[平仓] %s %s 已平仓，已实现盈亏 %.2f USDT | 连亏 %d 次",
            self.inst["instId"],
            self.pos.direction,
            closed_pnl,
            self.strategy.losing_streak,
        )
        if (
            self.strategy.losing_streak
            and self.strategy.losing_streak >= self.strategy.p.losing_streak_to_reduce
            and self.strategy.current_notional() != self.strategy.p.notional_per_order
        ):
            logger.warning(
                "[风控] 连续亏损 %d 次 → 名义价值降至 %.2f（EA 动态降仓）",
                self.strategy.losing_streak,
                self.strategy.current_notional() or 0,
            )

        # OCO 另一腿可能残留（通常自动撤销，保险清理）
        if self.pos.algo_id:
            try:
                self.client.cancel_algo(self.inst["instId"], self.pos.algo_id)
            except Exception:  # noqa: BLE001
                pass
        self.pos = SwapPositionState()

    # ==================================================================
    # 手动全平（停止机器人时可选）
    # ==================================================================
    def close_all(self) -> None:
        """市价平掉当前仓位并撤掉 OCO。"""
        if not self.pos.is_open:
            return
        side = "sell" if self.pos.direction == "long" else "buy"
        try:
            if self.pos.algo_id:
                self.client.cancel_algo(self.inst["instId"], self.pos.algo_id)
            self.client.market_order(
                self.inst["instId"], side, self.pos.contracts,
                td_mode=self.td_mode, reduce_only=True,
            )
            logger.info("[手动平仓] %s %d 张已市价平仓",
                        self.inst["instId"], self.pos.contracts)
        except ExchangeApiError as e:
            logger.error("[手动平仓] 失败: %s %s", e.code, e.msg)
        self.pos = SwapPositionState()

    # ==================================================================
    # 重启恢复：从交易所重建本地仓位镜像
    # ==================================================================
    def restore_from_exchange(self) -> None:
        """本地无仓位记录但交易所有仓位（进程重启场景）→ 重建镜像。"""
        if self.pos.is_open:
            return
        live = self.client.get_position(self.inst["instId"])
        if live is None:
            return
        direction = "long" if live["contracts"] > 0 else "short"
        sl = tp = 0.0
        algo_id = ""
        try:
            for algo in self.client.get_pending_algos(self.inst["instId"]):
                algo_id = str(algo.get("algoId", ""))
                sl = float(algo.get("slTriggerPx") or 0)
                tp = float(algo.get("tpTriggerPx") or 0)
                break
        except Exception:  # noqa: BLE001
            pass
        self.pos = SwapPositionState(
            direction=direction,
            contracts=abs(live["contracts"]),
            entry_price=live["avgPx"],
            sl_price=sl,
            tp_price=tp,
            algo_id=algo_id,
            atr=0.0,
            opened_at=int(time.time() * 1000),
            signal_reason="重启恢复",
        )
        logger.warning(
            "[恢复] %s 检测到交易所现存 %s %d 张(entry=%.2f)，已重建本地镜像 "
            "SL=%.2f TP=%.2f——ATR 上下文丢失，追踪止损将在下根K线重估",
            self.inst["instId"], direction, abs(live["contracts"]), live["avgPx"], sl, tp,
        )

    # ==================================================================
    def state_dict(self) -> Dict[str, Any]:
        return asdict(self.pos)

    def load_state(self, d: Dict[str, Any]) -> None:
        if d and d.get("direction"):
            self.pos = SwapPositionState(**{
                k: v for k, v in d.items() if k in SwapPositionState.__dataclass_fields__
            })
