"""
纸面撮合（Paper Broker）：不连接 OKX 私有接口的本地模拟执行环境。

- PaperSwapClient: 与 OkxSwapClient 同接口的本地撮合实现（开仓/OCO/追踪/
  平仓/权益/历史），实盘引擎可无缝切换 —— 无 API Key 也能跑通全链路
- SimulatedFeed: 模拟行情源（趋势段+波动率的随机游走），用于演示与回测

撮合假设（偏保守）：
- 市价单按当前价 ± 滑点(1 tick) 成交，双向收取 Taker 手续费
- K线内若止损与止盈同时触及，按**先触发止损**的悲观假设处理
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional

from .base_client import SwapClientMixin
from .swap_client import OkxSwapClient

logger = logging.getLogger(__name__)

# 常见 USDT 本位永续合约规则（与 OKX 实际规则一致；实盘时以接口返回为准）
DEFAULT_SWAP_INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    "BTC-USDT-SWAP": {
        "instId": "BTC-USDT-SWAP", "ctVal": 0.01, "ctValCcy": "BTC",
        "settleCcy": "USDT", "minSz": 1, "lotSz": 1, "tickSz": 0.1,
    },
    "ETH-USDT-SWAP": {
        "instId": "ETH-USDT-SWAP", "ctVal": 0.1, "ctValCcy": "ETH",
        "settleCcy": "USDT", "minSz": 1, "lotSz": 1, "tickSz": 0.01,
    },
}


def bar_seconds(bar: str) -> int:
    """OKX bar 周期 → 秒数。"""
    table = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600, "12H": 43200,
        "1D": 86400, "1W": 604800,
    }
    if bar not in table:
        raise ValueError(f"不支持的K线周期: {bar}（支持 {list(table)}）")
    return table[bar]


class PaperSwapClient(OkxSwapClient, SwapClientMixin):
    """本地纸面撮合客户端：接口与 OkxSwapClient 对齐。"""

    def __init__(
        self,
        initial_equity: float = 10_000.0,
        fee_rate: float = 0.0005,
        slippage_ticks: int = 1,
        instruments: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        super().__init__()  # 无密钥：公开行情接口仍可真实调用
        self.initial_equity = initial_equity
        self.fee_rate = fee_rate
        self.slippage_ticks = slippage_ticks
        self._instruments = dict(instruments or DEFAULT_SWAP_INSTRUMENTS)
        self._positions: Dict[str, Dict[str, Any]] = {}   # instId -> 仓位
        self._algos: Dict[str, Dict[str, Any]] = {}       # algoId -> OCO
        self._history: List[Dict[str, Any]] = []          # 平仓记录
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._price: Dict[str, float] = {}
        self._seq = 0
        self._now_ms = int(time.time() * 1000)

    # ------------------------------------------------------------------
    def set_price(self, inst_id: str, price: float) -> None:
        self._price[inst_id] = price

    def get_instrument(self, inst_id: str) -> Dict[str, Any]:
        if inst_id in self._instruments:
            return dict(self._instruments[inst_id])
        try:  # 实盘环境优先取真实规则
            return super().get_instrument(inst_id)
        except Exception:  # noqa: BLE001
            raise ValueError(f"纸面模式未内置合约规则且无法访问 OKX: {inst_id}")

    def get_ticker(self, inst_id: str) -> Dict[str, float]:
        if inst_id in self._price:
            p = self._price[inst_id]
            return {"last": p, "bid": p, "ask": p, "high24h": p, "low24h": p,
                    "vol24h": 0.0, "ts": self._now_ms}
        return super().get_ticker(inst_id)

    # ------------------------------------------------------------------
    # 下单 / 撮合
    # ------------------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def _slipped(self, inst_id: str, price: float, side: str) -> float:
        tick = self._instruments.get(inst_id, {}).get("tickSz", 0.01)
        slip = tick * self.slippage_ticks
        return price + slip if side == "buy" else price - slip

    def market_order(
        self, inst_id: str, side: str, sz: int,
        td_mode: str = "isolated", reduce_only: bool = False,
    ) -> str:
        price = self._price.get(inst_id)
        if not price:
            raise RuntimeError(f"{inst_id} 无价格数据")
        inst = self._instruments[inst_id]
        fill_price = self._slipped(inst_id, price, side)
        fee = sz * inst["ctVal"] * fill_price * self.fee_rate
        ord_id = self._next_id("ord")

        if reduce_only:
            self._reduce_position(inst_id, side, sz, fill_price, fee, "市价平仓")
        else:
            direction = "long" if side == "buy" else "short"
            if inst_id in self._positions:
                raise RuntimeError(f"{inst_id} 已有持仓，net 模式不可重复开仓")
            self._positions[inst_id] = {
                "direction": direction,
                "contracts": sz,
                "entry_price": fill_price,
                "open_fee": fee,
                "opened_at": self._now_ms,
            }
        self._orders[ord_id] = {
            "ordId": ord_id, "state": "filled", "avgPx": fill_price,
            "accFillSz": float(sz), "fee": fee, "side": side,
        }
        return ord_id

    def wait_order_filled(self, inst_id: str, ord_id: str, timeout: int = 20,
                          poll: float = 1.0) -> Dict[str, Any]:
        return dict(self._orders[ord_id])

    def get_order(self, inst_id: str, ord_id: str) -> Dict[str, Any]:
        return dict(self._orders[ord_id])

    # ------------------------------------------------------------------
    # OCO 止盈止损
    # ------------------------------------------------------------------
    def place_oco(self, inst_id: str, side: str, sz: int, tp_trigger: float,
                  sl_trigger: float, td_mode: str = "isolated") -> str:
        algo_id = self._next_id("algo")
        self._algos[algo_id] = {
            "algoId": algo_id, "instId": inst_id, "side": side, "sz": sz,
            "tpTriggerPx": tp_trigger, "slTriggerPx": sl_trigger,
        }
        return algo_id

    def get_pending_algos(self, inst_id: str) -> List[Dict[str, Any]]:
        return [dict(a) for a in self._algos.values() if a["instId"] == inst_id]

    def cancel_algo(self, inst_id: str, algo_id: str) -> None:
        self._algos.pop(algo_id, None)

    def amend_algo_sl(self, inst_id: str, algo_id: str, new_sl: float) -> bool:
        algo = self._algos.get(algo_id)
        if not algo:
            return False
        # 模拟交易所校验：多仓止损必须低于现价，空仓必须高于现价
        price = self._price.get(inst_id, 0)
        pos = self._positions.get(inst_id)
        if pos and price:
            if pos["direction"] == "long" and new_sl >= price:
                return False
            if pos["direction"] == "short" and new_sl <= price:
                return False
        algo["slTriggerPx"] = new_sl
        return True

    # ------------------------------------------------------------------
    # 持仓 / 权益 / 历史
    # ------------------------------------------------------------------
    def get_position(self, inst_id: str) -> Optional[Dict[str, Any]]:
        pos = self._positions.get(inst_id)
        if not pos:
            return None
        return {
            "instId": inst_id,
            "contracts": pos["contracts"] if pos["direction"] == "long" else -pos["contracts"],
            "avgPx": pos["entry_price"],
            "upl": self._unrealized(inst_id),
            "lever": "", "margin": 0.0, "liqPx": 0.0,
        }

    def _unrealized(self, inst_id: str) -> float:
        pos = self._positions.get(inst_id)
        if not pos:
            return 0.0
        price = self._price.get(inst_id, pos["entry_price"])
        inst = self._instruments[inst_id]
        sign = 1.0 if pos["direction"] == "long" else -1.0
        return (price - pos["entry_price"]) * pos["contracts"] * inst["ctVal"] * sign

    def get_equity(self, ccy: str = "USDT") -> float:
        realized = sum(h["realizedPnl"] for h in self._history)
        unreal = sum(self._unrealized(i) for i in self._positions)
        return self.initial_equity + realized + unreal

    def get_positions_history(self, inst_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = [h for h in self._history if h["instId"] == inst_id]
        return list(reversed(rows[-limit:]))

    # ------------------------------------------------------------------
    # K线驱动：SL/TP 触发检查（由引擎/回测每根K线调用）
    # ------------------------------------------------------------------
    def on_bar(self, inst_id: str, row: Dict[str, float]) -> None:
        """用已收盘K线的 high/low 检查 SL/TP 是否被触发（悲观：先止损）。"""
        self._now_ms = int(row.get("ts", self._now_ms))
        pos = self._positions.get(inst_id)
        if not pos:
            return
        for algo in list(self._algos.values()):
            if algo["instId"] != inst_id:
                continue
            sl, tp = algo["slTriggerPx"], algo["tpTriggerPx"]
            inst = self._instruments[inst_id]
            if pos["direction"] == "long":
                if sl > 0 and row["low"] <= sl:          # 先查止损（悲观）
                    exit_px = self._slipped(inst_id, sl, "sell")
                    self._reduce_position(inst_id, "sell", pos["contracts"],
                                          exit_px, 0.0, "止损", algo_id=algo["algoId"])
                    return
                if tp > 0 and row["high"] >= tp:
                    exit_px = self._slipped(inst_id, tp, "sell")
                    self._reduce_position(inst_id, "sell", pos["contracts"],
                                          exit_px, 0.0, "止盈", algo_id=algo["algoId"])
                    return
            else:
                if sl > 0 and row["high"] >= sl:
                    exit_px = self._slipped(inst_id, sl, "buy")
                    self._reduce_position(inst_id, "buy", pos["contracts"],
                                          exit_px, 0.0, "止损", algo_id=algo["algoId"])
                    return
                if tp > 0 and row["low"] <= tp:
                    exit_px = self._slipped(inst_id, tp, "buy")
                    self._reduce_position(inst_id, "buy", pos["contracts"],
                                          exit_px, 0.0, "止盈", algo_id=algo["algoId"])
                    return

    def _reduce_position(self, inst_id: str, side: str, sz: int, exit_price: float,
                         fee: float, reason: str, algo_id: str = "") -> None:
        pos = self._positions.get(inst_id)
        if not pos:
            return
        inst = self._instruments[inst_id]
        close_fee = sz * inst["ctVal"] * exit_price * self.fee_rate
        sign = 1.0 if pos["direction"] == "long" else -1.0
        pnl = (exit_price - pos["entry_price"]) * sz * inst["ctVal"] * sign \
            - pos.get("open_fee", 0.0) - close_fee
        self._history.append({
            "instId": inst_id,
            "direction": pos["direction"],
            "realizedPnl": pnl,
            "posId": self._next_id("pos"),
            "uTime": self._now_ms,
            "opened_at": pos.get("opened_at", self._now_ms),
            "type": 2,
            "exit_price": exit_price,
            "entry_price": pos["entry_price"],
            "contracts": sz,
            "reason": reason,
            "signal_reason": pos.get("signal_reason", ""),
        })
        logger.info(
            "[纸面平仓] %s %s %d张 entry=%.2f→exit=%.2f (%s) PnL=%.2f",
            inst_id, pos["direction"], sz, pos["entry_price"], exit_price, reason, pnl,
        )
        self._positions.pop(inst_id, None)
        for aid in list(self._algos):
            if self._algos[aid]["instId"] == inst_id:
                self._algos.pop(aid)

    # 记录开仓原因（供回测明细展示）
    def annotate_open(self, inst_id: str, reason: str) -> None:
        pos = self._positions.get(inst_id)
        if pos:
            pos["signal_reason"] = reason

    @property
    def trade_history(self) -> List[Dict[str, Any]]:
        return self._history


class SimulatedFeed:
    """模拟行情源：趋势段 + 波动率的随机游走，逐次调用推进一根K线。"""

    def __init__(
        self,
        inst_ids: List[str],
        bar: str = "15m",
        start_prices: Optional[Dict[str, float]] = None,
        history_bars: int = 320,
        seed: int = 42,
    ) -> None:
        self.bar = bar
        self.bar_sec = bar_seconds(bar)
        self.rng = random.Random(seed)
        self.start_prices = start_prices or {"BTC-USDT-SWAP": 65000.0, "ETH-USDT-SWAP": 3200.0}
        self._candles: Dict[str, List[Dict[str, Any]]] = {}
        self._regime: Dict[str, Dict[str, float]] = {}
        now_ms = int(time.time() * 1000)
        anchor = now_ms - (max(history_bars, 1) + 2) * self.bar_sec * 1000
        for inst_id in inst_ids:
            p0 = self.start_prices.get(inst_id, 100.0)
            self._regime[inst_id] = {"drift": 0.0, "vol": 0.004, "left": 0}
            self._candles[inst_id] = []
            price = p0
            for i in range(max(history_bars, 1)):
                ts = anchor + i * self.bar_sec * 1000
                row = self._gen_one(inst_id, price, ts)
                self._candles[inst_id].append(row)
                price = row["close"]

    def _gen_one(self, inst_id: str, price: float, ts: int) -> Dict[str, Any]:
        reg = self._regime[inst_id]
        if reg["left"] <= 0:  # 切换行情段：趋势方向与波动率
            reg["drift"] = self.rng.choice([-1, -0.5, 0, 0.5, 1]) * self.rng.uniform(0.0004, 0.0025)
            reg["vol"] = self.rng.uniform(0.0025, 0.008)
            reg["left"] = self.rng.randint(20, 90)
        reg["left"] -= 1
        ret = self.rng.gauss(reg["drift"], reg["vol"])
        # 随机插针：长影线
        wick = abs(self.rng.gauss(0, reg["vol"] * 0.8))
        open_ = price
        close = price * (1 + ret)
        high = max(open_, close) * (1 + wick)
        low = min(open_, close) * (1 - wick)
        vol = self.rng.uniform(50, 5000)
        return {"ts": ts, "open": open_, "high": high, "low": low,
                "close": close, "vol": vol, "volCcy": 0.0, "volQuote": 0.0}

    # ------------------------------------------------------------------
    def get_candles(self, inst_id: str, bar: str, limit: int = 300) -> List[Dict[str, Any]]:
        """每次调用推进一根新K线（模拟时间流逝），返回全部已收盘K线。"""
        self._advance(inst_id)
        return list(self._candles[inst_id][-limit:])

    def generate(self, inst_id: str, n: int) -> List[Dict[str, Any]]:
        """一次性生成 n 根新K线（回测数据生成用）。"""
        for _ in range(n):
            self._advance(inst_id)
        return list(self._candles[inst_id])

    def _advance(self, inst_id: str) -> Dict[str, Any]:
        rows = self._candles[inst_id]
        row = self._gen_one(inst_id, rows[-1]["close"], rows[-1]["ts"] + self.bar_sec * 1000)
        rows.append(row)
        return row

    def last_row(self, inst_id: str) -> Dict[str, Any]:
        return self._candles[inst_id][-1]
