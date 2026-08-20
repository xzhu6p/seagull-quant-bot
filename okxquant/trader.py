"""
交易执行器：把策略产生的订单意图（OrderIntent）变成真实的 OKX 订单。

- 市价单：买入按计价币金额（tgtCcy=quote_ccy），卖出按基础币数量
- 数量/价格按交易产品规则取整（lotSz / tickSz / minSz）
- 成交回报回填 PositionTracker，手续费折算入账
- 全部订单先过风控（RiskManager）
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional

from .client import OkxClient, OkxApiError
from .position import PositionTracker
from .risk import RiskManager

logger = logging.getLogger(__name__)

TAKER_FEE_RATE = 0.001  # 现货 Taker 手续费 0.1%，可在 config 覆盖


@dataclass
class OrderIntent:
    """策略输出的订单意图。quote_amount 与 base_amount 二选一。"""
    side: str                      # buy / sell
    quote_amount: float = 0.0      # 按金额下单（买入）
    base_amount: float = 0.0       # 按数量下单（卖出）
    reason: str = ""


@dataclass
class FillResult:
    success: bool
    side: str
    base_qty: float = 0.0
    avg_price: float = 0.0
    quote_amount: float = 0.0
    fee_quote: float = 0.0
    reason: str = ""


def floor_to_step(value: float, step: float) -> float:
    """按步长向下取整（数量用 lotSz，价格用 tickSz）。"""
    if step <= 0:
        return value
    return math.floor(value / step + 1e-9) * step


class Trader:
    def __init__(
        self,
        client: OkxClient,
        position: PositionTracker,
        risk: RiskManager,
        instrument: dict,
        fee_rate: float = TAKER_FEE_RATE,
    ) -> None:
        self.client = client
        self.position = position
        self.risk = risk
        self.inst = instrument
        self.fee_rate = fee_rate
        self.base_ccy = instrument["baseCcy"]
        self.quote_ccy = instrument["quoteCcy"]

    # ------------------------------------------------------------------
    def execute(self, intents: List[OrderIntent], df_recent=None) -> List[FillResult]:
        """批量执行订单意图（逐笔过风控）。"""
        results: List[FillResult] = []
        for intent in intents:
            try:
                res = self._execute_one(intent, df_recent)
            except OkxApiError as e:
                logger.error("[交易] OKX 拒单: %s %s", e.code, e.msg)
                results.append(FillResult(False, intent.side, reason=f"OKX {e.code}: {e.msg}"))
            except Exception as e:  # noqa: BLE001
                logger.exception("[交易] 下单异常: %s", e)
                results.append(FillResult(False, intent.side, reason=str(e)))
            else:
                results.append(res)
        return results

    def _execute_one(self, intent: OrderIntent, df_recent=None) -> FillResult:
        ticker = self.client.get_ticker(self.inst["instId"])
        price = ticker["last"]

        if intent.side == "buy":
            amount = intent.quote_amount
            if amount < self.inst["minSz"] * price and amount < 1.0:
                return FillResult(False, "buy", reason="金额过小")
            ok, why = self.risk.check_order("buy", amount, price, df_recent)
            if not ok:
                logger.info("[风控拦截] 买入 %.2f USDT 被拒: %s", amount, why)
                return FillResult(False, "buy", quote_amount=amount, reason=why)

            # 市价买入：sz 按计价币金额，金额按 lotSz(quote 精度通常0.01)取整
            step = max(self.inst["lotSz"] * price, 0.01)
            sz = floor_to_step(amount, step)
            if sz <= 0:
                return FillResult(False, "buy", reason="取整后金额为 0")
            ord_id = self.client.place_order(
                self.inst["instId"], "buy", "market", f"{sz:.2f}", tgt_ccy="quote_ccy"
            )
            info = self.client.wait_order_filled(self.inst["instId"], ord_id)
            if info["state"] != "filled" or info["accFillSz"] <= 0:
                self.client.cancel_order(self.inst["instId"], ord_id)
                return FillResult(False, "buy", reason=f"未完全成交(state={info['state']})")

            filled_quote = info["accFillSz"]  # tgtCcy=quote_ccy 时 accFillSz 为金额
            avg_px = info["avgPx"] or price
            base_qty = filled_quote / avg_px if avg_px > 0 else 0.0
            fee = filled_quote * self.fee_rate
            self.position.on_fill("buy", base_qty, avg_px, fee)
            self.risk.mark_order_time()
            logger.info(
                "[成交] 买入 %s: %.8f @ %.2f (%.2f USDT) ordId=%s",
                self.inst["instId"], base_qty, avg_px, filled_quote, ord_id,
            )
            return FillResult(True, "buy", base_qty, avg_px, filled_quote, fee)

        # ---------------- 卖出 ----------------
        qty = intent.base_amount
        if qty <= 0 and self.position.state.base_qty > 0:
            qty = self.position.state.base_qty  # 默认全部卖出
        qty = min(qty, self.position.state.base_qty)
        qty = floor_to_step(qty, self.inst["lotSz"])
        if qty < self.inst["minSz"]:
            return FillResult(False, "sell", reason="可卖数量不足最小交易量")

        ok, why = self.risk.check_order(
            "sell", qty * price, price, df_recent
        )
        if not ok:
            logger.info("[风控拦截] 卖出被拒: %s", why)
            return FillResult(False, "sell", base_amount=qty, reason=why)

        ord_id = self.client.place_order(
            self.inst["instId"], "sell", "market", f"{qty:.8f}".rstrip("0")
        )
        info = self.client.wait_order_filled(self.inst["instId"], ord_id)
        if info["state"] != "filled" or info["accFillSz"] <= 0:
            self.client.cancel_order(self.inst["instId"], ord_id)
            return FillResult(False, "sell", reason=f"未完全成交(state={info['state']})")

        avg_px = info["avgPx"] or price
        filled_quote = info["accFillSz"] * avg_px
        fee = filled_quote * self.fee_rate
        self.position.on_fill("sell", info["accFillSz"], avg_px, fee)
        self.risk.mark_order_time()
        logger.info(
            "[成交] 卖出 %s: %.8f @ %.2f (%.2f USDT) ordId=%s",
            self.inst["instId"], info["accFillSz"], avg_px, filled_quote, ord_id,
        )
        return FillResult(True, "sell", info["accFillSz"], avg_px, filled_quote, fee)
