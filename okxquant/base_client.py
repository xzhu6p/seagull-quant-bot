"""
合约客户端统一接口（多平台支持）。

定义 SwapClientBase 协议——所有交易所客户端（OKX / 币安 / 纸面）都满足这组方法签名。
SwapTrader 和 SeagullEngine 只依赖这个协议，与具体交易所解耦。

关键设计：
- inst 字段统一为 {instId, ctVal, ctValCcy, settleCcy, minSz, lotSz, tickSz}
- K线统一为 {ts, open, high, low, close, vol, volCcy, volQuote}，升序
- 持仓统一为 {instId, contracts(带方向), avgPx, upl, lever, margin, liqPx}
- 平仓历史统一为 {instId, direction, realizedPnl, posId, uTime, type}
- OCO：OKX 用原生 OCO，币安用 TP+SL 两个条件单模拟；统一返回 algo_id 字符串
  （币安格式 "tp_id|sl_id"，外部不感知）
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class SwapClientBase(Protocol):
    """合约客户端统一接口。"""

    # ── 合约规则 ──
    def get_instrument(self, inst_id: str) -> Dict[str, Any]: ...

    # ── 行情 ──
    def get_ticker(self, inst_id: str) -> Dict[str, float]: ...

    def get_candles(
        self, inst_id: str, bar: str = "1H", limit: int = 300, **kw
    ) -> List[Dict[str, Any]]: ...

    def get_candles_paged(
        self, inst_id: str, bar: str, total: int = 1000
    ) -> List[Dict[str, Any]]: ...

    # ── 账户设置 ──
    def set_position_mode_net(self) -> None: ...

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "isolated") -> None: ...

    # ── 交易 ──
    def market_order(
        self, inst_id: str, side: str, sz: int,
        td_mode: str = "isolated", reduce_only: bool = False,
    ) -> str: ...

    def place_oco(
        self, inst_id: str, side: str, sz: int,
        tp_trigger: float, sl_trigger: float, td_mode: str = "isolated",
    ) -> str: ...

    def get_pending_algos(self, inst_id: str) -> List[Dict[str, Any]]: ...

    def cancel_algo(self, inst_id: str, algo_id: str) -> None: ...

    def amend_algo_sl(self, inst_id: str, algo_id: str, new_sl: float) -> bool: ...

    # ── 订单生命周期 ──
    def wait_order_filled(self, inst_id: str, ord_id: str, **kw) -> Dict[str, Any]: ...

    def cancel_order(self, inst_id: str, ord_id: str) -> None: ...

    # ── 持仓 / 账户 ──
    def get_position(self, inst_id: str) -> Optional[Dict[str, Any]]: ...

    def get_equity(self, ccy: str = "USDT") -> float: ...

    def get_positions_history(self, inst_id: str, limit: int = 20) -> List[Dict[str, Any]]: ...

    # ── 工具 ──
    @staticmethod
    def floor_to_step(value: float, step: float) -> float: ...

    def contracts_for_notional(self, inst: Dict[str, Any], notional: float, price: float) -> int: ...

    def round_px(self, inst: Dict[str, Any], price: float, up: bool = False) -> float: ...


class SwapClientMixin:
    """客户端工具方法混入：所有平台共用的张数换算 / 价格取整。

    子类只需实现 get_instrument 拉到 {ctVal, minSz, lotSz, tickSz} 即可复用。
    """

    @staticmethod
    def floor_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        return math.floor(value / step + 1e-9) * step

    def contracts_for_notional(self, inst: Dict[str, Any], notional: float, price: float) -> int:
        """按名义价值(USDT)换算合约张数（向下取整，至少校验 minSz）。"""
        if price <= 0 or inst.get("ctVal", 0) <= 0:
            return 0
        raw = notional / (inst["ctVal"] * price)
        qty = int(self.floor_to_step(raw, inst.get("lotSz", 1)))
        if qty < int(inst.get("minSz", 1)):
            return 0
        return qty

    def round_px(self, inst: Dict[str, Any], price: float, up: bool = False) -> float:
        tick = inst.get("tickSz", 0)
        if tick <= 0:
            return price
        n = price / tick
        return (math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * tick
