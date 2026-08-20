"""
OKX USDT 永续合约（SWAP）接口扩展：继承 OkxClient，复用签名/重试/时间校准。

覆盖 Seagull 策略所需的全部合约能力：
- 合约规则（张数面值 ctVal / 最小张数 / 价格精度 tickSz）
- 账户持仓模式（net 单向）、杠杆设置
- 市价开仓 / 平仓（reduceOnly）
- OCO 条件单（止盈止损，交易所侧触发——机器人掉线也生效，保命优先）
- 修改/撤销策略委托（追踪止损移动 SL 触发价）
- 持仓查询、账户权益、平仓历史（连续亏损统计）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base_client import SwapClientMixin
from .client import OkxApiError, OkxClient

logger = logging.getLogger(__name__)


class OkxSwapClient(OkxClient, SwapClientMixin):
    """OKX 永续合约客户端（USDT 本位）。"""

    # ------------------------------------------------------------------
    # 合约规则 / 账户设置
    # ------------------------------------------------------------------
    def get_instrument(self, inst_id: str) -> Dict[str, Any]:
        """合约规则：ctVal=每张面值(币), minSz/lotSz=张数步长, tickSz=价格步长。"""
        resp = self._request(
            "GET",
            "/api/v5/public/instruments",
            {"instType": "SWAP", "instId": inst_id},
            auth=False,
        )
        if not resp["data"]:
            raise OkxApiError("-1", f"未找到合约 {inst_id}")
        d = resp["data"][0]
        return {
            "instId": d["instId"],
            "ctVal": float(d["ctVal"]),          # 每张面值（如 BTC 0.01）
            "ctValCcy": d["ctValCcy"],           # 面值币种（BTC）
            "settleCcy": d.get("settleCcy", "USDT"),
            "minSz": float(d["minSz"]),          # 最小张数（通常 1）
            "lotSz": float(d["lotSz"]),          # 张数步长（通常 1）
            "tickSz": float(d["tickSz"]),        # 价格步长
        }

    def set_position_mode_net(self) -> None:
        """切换为单向持仓（net_mode）。仅无持仓时可用，失败仅告警。"""
        try:
            self._request("POST", "/api/v5/account/set-position-mode", body={"posMode": "net_mode"})
            logger.info("账户持仓模式已设置为单向(net_mode)")
        except OkxApiError as e:
            logger.warning("设置单向持仓模式失败(%s %s)——若有持仓请先平仓", e.code, e.msg)

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "isolated") -> None:
        """设置杠杆倍数（逐仓/全仓）。"""
        self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body={"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode},
        )
        logger.info("%s 杠杆已设置为 %dx (%s)", inst_id, lever, mgn_mode)

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------
    def market_order(
        self,
        inst_id: str,
        side: str,
        sz: int,
        td_mode: str = "isolated",
        reduce_only: bool = False,
    ) -> str:
        """合约市价单。net 模式：开多=buy / 开空=sell；平仓传 reduce_only=True。

        sz 为合约张数（整数）。
        """
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "market",
            "sz": str(int(sz)),
        }
        if reduce_only:
            body["reduceOnly"] = True
        resp = self._request("POST", "/api/v5/trade/order", body=body)
        d = resp["data"][0]
        if str(d.get("sCode", "0")) != "0":
            raise OkxApiError(str(d["sCode"]), str(d.get("sMsg", "下单失败")), d)
        return str(d["ordId"])

    def place_oco(
        self,
        inst_id: str,
        side: str,
        sz: int,
        tp_trigger: float,
        sl_trigger: float,
        td_mode: str = "isolated",
    ) -> str:
        """下 OCO 止盈止损条件单（市价触发，reduceOnly）。

        side = 平仓方向：多仓传 sell，空仓传 buy。
        触发后市价平仓（tpOrdPx/slOrdPx = -1）。
        返回 algoId。
        """
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "oco",
            "sz": str(int(sz)),
            "tpTriggerPx": f"{tp_trigger}",
            "tpOrdPx": "-1",
            "slTriggerPx": f"{sl_trigger}",
            "slOrdPx": "-1",
            "reduceOnly": True,
        }
        resp = self._request("POST", "/api/v5/trade/order-algo", body=body)
        d = resp["data"][0]
        if str(d.get("sCode", "0")) != "0":
            raise OkxApiError(str(d["sCode"]), str(d.get("sMsg", "OCO下单失败")), d)
        return str(d.get("algoId", ""))

    def get_pending_algos(self, inst_id: str) -> List[Dict[str, Any]]:
        """当前挂起的 OCO 策略单。"""
        resp = self._request(
            "GET",
            "/api/v5/trade/orders-algo-pending",
            {"ordType": "oco", "instId": inst_id},
        )
        return resp.get("data", [])

    def cancel_algo(self, inst_id: str, algo_id: str) -> None:
        """撤销策略委托。"""
        try:
            self._request(
                "POST",
                "/api/v5/trade/cancel-algos",
                body=[{"algoId": algo_id, "instId": inst_id}],
            )
        except OkxApiError as e:
            if e.code not in ("51400", "51401", "51402", "51603"):
                raise

    def amend_algo_sl(self, inst_id: str, algo_id: str, new_sl: float) -> bool:
        """修改 OCO 的止损触发价（追踪止损用）。失败返回 False（原止损仍有效）。"""
        try:
            self._request(
                "POST",
                "/api/v5/trade/amend-algos",
                body={"algoId": algo_id, "instId": inst_id, "slTriggerPx": f"{new_sl}"},
            )
            return True
        except OkxApiError as e:
            logger.warning("修改止损触发价失败(%s %s)，保留原止损", e.code, e.msg)
            return False

    # ------------------------------------------------------------------
    # 持仓 / 账户
    # ------------------------------------------------------------------
    def get_position(self, inst_id: str) -> Optional[Dict[str, Any]]:
        """查询某合约当前持仓（net 模式）。无持仓返回 None。

        pos: 正数=多头张数，负数=空头张数
        """
        resp = self._request("GET", "/api/v5/account/positions", {"instId": inst_id})
        for p in resp.get("data", []):
            pos = float(p.get("pos") or 0)
            if abs(pos) > 0:
                return {
                    "instId": inst_id,
                    "contracts": int(pos),              # 带方向：正多负空
                    "avgPx": float(p.get("avgPx") or 0),
                    "upl": float(p.get("upl") or 0),     # 未实现盈亏(USDT)
                    "lever": p.get("lever", ""),
                    "margin": float(p.get("margin") or 0),
                    "liqPx": float(p.get("liqPx") or 0),
                }
        return None

    def get_equity(self, ccy: str = "USDT") -> float:
        """账户权益（USDT 计），日内熔断的基准。"""
        resp = self._request("GET", "/api/v5/account/balance", {"ccy": ccy})
        details = resp["data"][0].get("details", [])
        for d in details:
            if d["ccy"] == ccy:
                return float(d.get("eq") or 0)
        return 0.0

    def get_positions_history(self, inst_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """最近平仓历史（按时间倒序），用于连续亏损统计。

        realizedPnl: 净已实现盈亏（扣手续费/资金费）。
        """
        resp = self._request(
            "GET",
            "/api/v5/account/positions-history",
            {"instType": "SWAP", "instId": inst_id, "limit": str(min(limit, 100))},
        )
        out = []
        for h in resp.get("data", []):
            out.append(
                {
                    "instId": h.get("instId"),
                    "direction": h.get("direction"),       # long / short
                    "realizedPnl": float(h.get("realizedPnl") or 0),
                    "posId": str(h.get("posId", "")),
                    "uTime": int(h.get("uTime") or 0),
                    "type": h.get("type"),                 # 2全部平仓 3强平 4强减 5ADL
                }
            )
        return out

    # ------------------------------------------------------------------
    # 工具方法：张数换算 / 价格取整 已由 SwapClientMixin 提供
    # ------------------------------------------------------------------
