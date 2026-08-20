"""
OKX V5 REST API 客户端。

- HMAC-SHA256 签名认证（OK-ACCESS-KEY / SIGN / TIMESTAMP / PASSPHRASE）
- 自动与服务器时间校准（偏差超过 30 秒签名会失败）
- 模拟盘支持（x-simulated-trading: 1）
- 429 / 5xx / 网络错误自动指数退避重试
- K线分页拉取、订单生命周期查询、余额查询、下单/撤单

接口文档: https://www.okx.com/docs-v5/zh-cn/
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from .exchange_error import ExchangeApiError

logger = logging.getLogger(__name__)

KLINE_FIELDS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volQuote"]


class OkxApiError(ExchangeApiError):
    """OKX 接口业务错误（code != 0）。继承统一异常，trader 无需感知具体平台。"""

    def __init__(self, code: str, msg: str, data: Any = None):
        super().__init__(code, msg, data)


class OkxClient:
    """OKX V5 REST 客户端（线程不安全，每个策略实例使用独立客户端）。"""

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        simulated: bool = True,
        base_url: str = "https://www.okx.com",
        timeout: int = 15,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.simulated = simulated
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._time_offset_ms = 0
        if api_key:
            self.sync_time()

    # ------------------------------------------------------------------
    # 基础设施：时间 / 签名 / 请求
    # ------------------------------------------------------------------
    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def sync_time(self) -> int:
        """与 OKX 服务器时间校准，返回偏差毫秒数。"""
        path = "/api/v5/public/time"
        resp = self._request("GET", path, auth=False)
        server_ms = int(resp["data"][0]["ts"])
        self._time_offset_ms = server_ms - self._now_ms()
        logger.info("OKX 服务器时间校准完成，本地偏差 %d ms", self._time_offset_ms)
        return self._time_offset_ms

    def _timestamp(self) -> str:
        """签名用 ISO 8601 时间戳，如 2024-05-01T08:00:00.123Z。"""
        ms = self._now_ms() + self._time_offset_ms
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        message = f"{timestamp}{method}{request_path}{body}"
        mac = hmac.new(
            self.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        """发送请求并解析响应；失败自动重试。

        注意：OKX 签名要求 requestPath 包含 query string，因此 query
        必须由本方法统一构造，避免与 requests 内部编码不一致。
        """
        request_path = path
        if params:
            # 过滤 None 值，保持插入顺序（签名与 URL 使用同一字符串）
            pairs = [(k, v) for k, v in params.items() if v is not None]
            request_path = f"{path}?{urlencode(pairs)}"

        body_str = json.dumps(body, separators=(",", ":")) if body else ""

        headers = {"Content-Type": "application/json"}
        if auth:
            if not self.api_key:
                raise OkxApiError("-1", "缺少 API Key，无法调用私有接口")
            ts = self._timestamp()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": self._sign(ts, method.upper(), request_path, body_str),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                }
            )
        if self.simulated:
            headers["x-simulated-trading"] = "1"

        url = f"{self.base_url}{request_path}"
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=headers,
                    data=body_str.encode("utf-8") if body_str else None,
                    timeout=self.timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                result = resp.json()
                code = str(result.get("code", "0"))
                if code != "0":
                    # 50113: 签名失效; 50102: 时间偏差过大 → 重新校准后重试
                    if code in ("50102", "50113") and attempt < self.max_retries:
                        logger.warning("OKX 签名/时间错误(code=%s)，重新校准时间后重试", code)
                        try:
                            self.sync_time()
                        except Exception:
                            pass
                        last_err = OkxApiError(code, result.get("msg", ""), result.get("data"))
                        time.sleep(1)
                        continue
                    raise OkxApiError(code, result.get("msg", ""), result.get("data"))
                return result
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                last_err = e
                if attempt < self.max_retries:
                    wait = 0.5 * (2 ** attempt)
                    logger.warning("请求失败(%s)，%.1fs 后重试 %s", e, wait, request_path)
                    time.sleep(wait)
        raise RuntimeError(f"请求 OKX 失败（已重试 {self.max_retries} 次）: {request_path}") from last_err

    # ------------------------------------------------------------------
    # 公共行情接口
    # ------------------------------------------------------------------
    def get_ticker(self, inst_id: str) -> Dict[str, float]:
        """最新成交价信息。"""
        resp = self._request("GET", "/api/v5/market/ticker", {"instId": inst_id}, auth=False)
        d = resp["data"][0]
        return {
            "last": float(d["last"]),
            "bid": float(d["bidPx"] or 0),
            "ask": float(d["askPx"] or 0),
            "high24h": float(d.get("high24h") or 0),
            "low24h": float(d.get("low24h") or 0),
            "vol24h": float(d.get("vol24h") or 0),
            "ts": int(d["ts"]),
        }

    def get_candles(
        self,
        inst_id: str,
        bar: str = "1H",
        limit: int = 300,
        after: Optional[str] = None,
        history: bool = False,
    ) -> List[Dict[str, Any]]:
        """拉取K线，返回按时间**升序**的字典列表。

        OKX 返回最新在前，这里统一翻转为升序，便于直接喂给策略。
        after: 传入某根K线的 ts，返回比它更早（更旧）的数据，用于翻页。
        history=True 时使用 /market/candles-history（可取更久远数据）。
        """
        path = "/api/v5/market/candles-history" if history else "/api/v5/market/candles"
        params: Dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))}
        if after:
            params["after"] = after
        resp = self._request("GET", path, params, auth=False)
        rows = []
        for raw in resp.get("data", []):
            row = dict(zip(KLINE_FIELDS, raw))
            row["ts"] = int(row["ts"])
            for f in KLINE_FIELDS[1:]:
                row[f] = float(row[f])
            rows.append(row)
        return list(reversed(rows))  # 升序

    def get_candles_paged(
        self, inst_id: str, bar: str, total: int = 1000
    ) -> List[Dict[str, Any]]:
        """分页拉取足够多的历史K线（每页最多 300 根）。"""
        collected: List[Dict[str, Any]] = []
        after: Optional[str] = None
        use_history = False
        while len(collected) < total:
            batch = self.get_candles(inst_id, bar, limit=300, after=after, history=use_history)
            if not batch:
                break
            collected = batch + collected  # batch 是升序，且整体比 collected 旧
            after = str(batch[0]["ts"])
            if len(batch) < 300 and not use_history:
                # /market/candles 只有近期数据，切到 history 接口继续翻
                use_history = True
                batch_hist = self.get_candles(
                    inst_id, bar, limit=1, after=after, history=True
                )
                if not batch_hist:
                    break
        return collected[-total:]

    def get_instrument(self, inst_id: str) -> Dict[str, Any]:
        """交易产品规则：最小数量 / 数量精度 / 价格精度，下单前必需。"""
        resp = self._request(
            "GET",
            "/api/v5/public/instruments",
            {"instType": "SPOT", "instId": inst_id},
            auth=False,
        )
        if not resp["data"]:
            raise OkxApiError("-1", f"未找到交易对 {inst_id}")
        d = resp["data"][0]
        return {
            "instId": d["instId"],
            "baseCcy": d["baseCcy"],
            "quoteCcy": d["quoteCcy"],
            "minSz": float(d["minSz"]),
            "lotSz": float(d["lotSz"]),
            "tickSz": float(d["tickSz"]),
        }

    # ------------------------------------------------------------------
    # 私有接口：账户 / 交易
    # ------------------------------------------------------------------
    def get_balance(self, ccy: str = "USDT") -> Dict[str, float]:
        """查询币种余额（现货账户）。"""
        resp = self._request("GET", "/api/v5/account/balance", {"ccy": ccy})
        details = resp["data"][0]["details"]
        for item in details:
            if item["ccy"] == ccy:
                return {
                    "total": float(item["cashBal"] or 0),
                    "available": float(item["availBal"] or 0),
                    "frozen": float(item["frozenBal"] or 0),
                }
        return {"total": 0.0, "available": 0.0, "frozen": 0.0}

    def place_order(
        self,
        inst_id: str,
        side: str,
        ord_type: str = "market",
        sz: str = "",
        px: str = "",
        td_mode: str = "cash",
        tgt_ccy: str = "",
    ) -> str:
        """下单，返回 ordId。

        现货市价买入默认 sz 按计价币金额（USDT），tgtCcy=quote_ccy；
        市价卖出 sz 按基础币数量（tgtCcy=base_ccy）。
        限价单 sz 一律按基础币数量。
        """
        body: Dict[str, str] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }
        if px:
            body["px"] = px
        if tgt_ccy:
            body["tgtCcy"] = tgt_ccy
        resp = self._request("POST", "/api/v5/trade/order", body=body)
        d = resp["data"][0]
        if d.get("sCode") and str(d["sCode"]) != "0":
            raise OkxApiError(str(d["sCode"]), str(d.get("sMsg", "下单失败")), d)
        return str(d["ordId"])

    def get_order(self, inst_id: str, ord_id: str) -> Dict[str, Any]:
        """查询订单状态与成交信息。"""
        resp = self._request(
            "GET",
            "/api/v5/trade/order",
            {"instId": inst_id, "ordId": ord_id},
        )
        d = resp["data"][0]
        return {
            "ordId": d["ordId"],
            "state": d["state"],  # live/partially_filled/filled/canceled
            "avgPx": float(d.get("avgPx") or 0),
            "accFillSz": float(d.get("accFillSz") or 0),
            "fillPx": float(d.get("fillPx") or 0),
            "fillSz": float(d.get("fillSz") or 0),
            "fee": float(d.get("fee") or 0),
            "pnl": float(d.get("pnl") or 0),
            "side": d["side"],
        }

    def wait_order_filled(
        self, inst_id: str, ord_id: str, timeout: int = 20, poll: float = 1.0
    ) -> Dict[str, Any]:
        """轮询等待订单成交（市价单通常立即成交），返回成交结果。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self.get_order(inst_id, ord_id)
            if info["state"] in ("filled", "canceled"):
                return info
            time.sleep(poll)
        return self.get_order(inst_id, ord_id)

    def cancel_order(self, inst_id: str, ord_id: str) -> None:
        """撤销订单（订单已成交时会报错，忽略即可）。"""
        try:
            self._request(
                "POST",
                "/api/v5/trade/cancel-order",
                body={"instId": inst_id, "ordId": ord_id},
            )
        except OkxApiError as e:
            if e.code not in ("51400", "51401", "51402"):  # 订单不存在/已成交/已撤
                raise

    def get_pending_orders(self, inst_id: str) -> List[Dict[str, Any]]:
        """当前挂单列表。"""
        resp = self._request(
            "GET", "/api/v5/trade/orders-pending", {"instType": "SPOT", "instId": inst_id}
        )
        return resp.get("data", [])
