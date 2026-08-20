"""
币安 USDT 本位永续合约（USDⓈ-M Futures）客户端。

接口与 OkxSwapClient 对齐（同一 SwapClientBase 协议），
SeagullEngine / SwapTrader 无需感知平台差异，配置 exchange=binance 即可切换。

覆盖 Seagull 策略所需的全部合约能力：
- 合约规则（contractSize=每张面值, pricePrecision=tickSize, 数量精度）
- 账户保证金模式（单向持仓）、杠杆设置
- 市价开仓 / 平仓（reduceOnly）
- OCO 止盈止损：币安永续无原生 OCO，用 TAKE_PROFIT_MARKET + STOP_MARKET
  两个 reduce-only 条件单组合实现；algo_id 格式 "tp_id|sl_id"，对外透明
- 修改止损触发价（追踪止损用，改 SL 那腿的 stopPrice）
- 持仓查询、账户权益、平仓历史（收入实现，用于连亏统计）

接口文档: https://binance-docs.github.io/apidocs/futures/en/
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from .base_client import SwapClientMixin
from .exchange_error import ExchangeApiError

logger = logging.getLogger(__name__)

# 币安 K线字段（与 OKX 统一）
KLINE_FIELDS = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volQuote"]

# 现货/合约 symbol → 统一 OKX 风格 instId 的映射（方便策略与 paper 对齐）
# 例：BTCUSDT ↔ BTC-USDT-SWAP。引擎传入哪个就用哪个，binance 客户端自动识别。
_BINANCE_SYMBOL_CACHE: Dict[str, Dict[str, Any]] = {}


class BinanceSwapClient(SwapClientMixin):
    """币安 USDT 本位永续合约客户端。"""

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        testnet: bool = False,
        base_url: str = "",
        timeout: int = 15,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.testnet = testnet
        if base_url:
            self.base_url = base_url.rstrip("/")
        else:
            self.base_url = (
                "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._time_offset_ms = 0
        if api_key:
            try:
                self.sync_time()
            except Exception as e:  # noqa: BLE001
                logger.warning("币安时间校准失败(%s)，签名可能出错", e)

    # ==================================================================
    # 基础设施：时间 / 签名 / 请求
    # ==================================================================
    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def sync_time(self) -> int:
        """与币安服务器时间校准。"""
        resp = self._request("GET", "/fapi/v1/time", auth=False)
        server_ms = int(resp["serverTime"])
        self._time_offset_ms = server_ms - self._now_ms()
        logger.info("币安服务器时间校准完成，本地偏差 %d ms", self._time_offset_ms)
        return self._time_offset_ms

    def _sign(self, timestamp: int, query: str) -> str:
        """币安 HMAC-SHA256 签名：HMAC(secretKey, timestamp + query)。"""
        message = f"{timestamp}{query}"
        return hmac.new(
            self.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Any:
        """发送请求并解析响应；失败自动重试。"""
        params = dict(params or {})
        # 鉴权接口加签名
        if auth:
            if not self.api_key:
                raise ExchangeApiError("-1", "缺少 API Key，无法调用私有接口")
            ts = self._now_ms() + self._time_offset_ms
            params["timestamp"] = ts
            query = urlencode(params)
            params["signature"] = self._sign(ts, query)

        query = urlencode(params)
        url = f"{self.base_url}{path}?{query}"

        headers = {"Content-Type": "application/json"}
        if auth:
            headers["X-MBX-APIKEY"] = self.api_key

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.request(method, url, headers=headers, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                # 币安错误：HTTP 400-499 + body {"code":-1121,"msg":"..."}
                if 400 <= resp.status_code < 500:
                    try:
                        err = resp.json()
                        code = str(err.get("code", resp.status_code))
                        msg = err.get("msg", "")
                        # -1021 时间戳偏差 → 重新校准后重试
                        if code == "-1021" and attempt < self.max_retries:
                            logger.warning("币安时间戳偏差(code=%s)，重新校准后重试", code)
                            try:
                                self.sync_time()
                            except Exception:
                                pass
                            time.sleep(1)
                            continue
                        raise ExchangeApiError(code, msg, err)
                    except ValueError:
                        raise ExchangeApiError(str(resp.status_code), resp.text[:200])
                return resp.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                last_err = e
                if attempt < self.max_retries:
                    wait = 0.5 * (2 ** attempt)
                    logger.warning("请求失败(%s)，%.1fs 后重试 %s", e, wait, path)
                    time.sleep(wait)
        raise RuntimeError(f"请求币安失败（已重试 {self.max_retries} 次）: {path}") from last_err

    # ==================================================================
    # symbol 适配：统一 instId ↔ binance symbol
    # ==================================================================
    @staticmethod
    def _to_binance_symbol(inst_id: str) -> str:
        """BTC-USDT-SWAP → BTCUSDT（去分隔符与 SWAP 后缀）。"""
        s = inst_id.upper()
        for suffix in ("-USDT-SWAP", "-SWAP", "-PERP"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                break
        s = s.replace("-", "")
        if not s.endswith("USDT"):
            s = s + "USDT"
        return s

    # ==================================================================
    # 合约规则
    # ==================================================================
    def get_instrument(self, inst_id: str) -> Dict[str, Any]:
        """合约规则：映射到 OKX 风格字段。

        语义对齐：币安永续 quantity 是浮点币数（如 0.001 BTC），OKX sz 是整数张数。
        把币安 stepSize 作为"内部单位面值 ctVal"，这样 SwapTrader 用整数 contracts
        管理仓位，下单时 quantity = contracts * ctVal。例：
          BTCUSDT stepSize=0.001 → ctVal=0.001，contracts=15 → quantity=0.015 BTC
        """
        symbol = self._to_binance_symbol(inst_id)
        if symbol in _BINANCE_SYMBOL_CACHE:
            return dict(_BINANCE_SYMBOL_CACHE[symbol])
        resp = self._request("GET", "/fapi/v1/exchangeInfo", auth=False)
        for s in resp.get("symbols", []):
            if s["symbol"] == symbol:
                tick_sz = float(s.get("pricePrecision", 2))
                lot_sz = 1.0
                min_sz = 1.0
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        lot_sz = float(f["stepSize"])
                        min_sz = float(f["minQty"])
                # 币安：ctVal = stepSize（每内部单位 = stepSize 个币）
                # contracts（整数）× ctVal = 实际下单 quantity
                inst = {
                    "instId": inst_id,
                    "symbol": symbol,
                    "ctVal": lot_sz,                 # 每内部单位对应的币数
                    "ctValCcy": s.get("baseAsset", ""),
                    "settleCcy": "USDT",
                    "minSz": 1.0,                    # 内部最小 1 个单位
                    "lotSz": 1.0,                    # 内部步长 1
                    "tickSz": tick_sz,
                    "binance_step_size": lot_sz,     # 下单时换算用
                }
                _BINANCE_SYMBOL_CACHE[symbol] = inst
                return dict(inst)
        raise ExchangeApiError("-1", f"未找到合约 {inst_id} (symbol={symbol})")

    # ==================================================================
    # 账户设置
    # ==================================================================
    def set_position_mode_net(self) -> None:
        """切换为单向持仓。币安 dualSidePosition=false 即单向。"""
        try:
            self._request("POST", "/fapi/v1/positionSide/dual",
                          params={"dualSidePosition": "false"})
            logger.info("账户持仓模式已设置为单向(hedge=false)")
        except ExchangeApiError as e:
            # -4059: 模式未变（已经是单向），忽略
            if e.code != "-4059":
                logger.warning("设置单向持仓模式失败(%s %s)——若有持仓请先平仓", e.code, e.msg)

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "isolated") -> None:
        """设置杠杆倍数。币安逐仓/全仓由 mgn_mode 映射。"""
        symbol = self._to_binance_symbol(inst_id)
        self._request("POST", "/fapi/v1/leverage",
                      params={"symbol": symbol, "leverage": str(lever),
                              "marginType": "ISOLATED" if mgn_mode == "isolated" else "CROSSED"})
        logger.info("%s(%s) 杠杆已设置为 %dx (%s)", inst_id, symbol, lever, mgn_mode)

    # ==================================================================
    # 行情
    # ==================================================================
    def get_ticker(self, inst_id: str) -> Dict[str, float]:
        symbol = self._to_binance_symbol(inst_id)
        d = self._request("GET", "/fapi/v1/ticker/price",
                          params={"symbol": symbol}, auth=False)
        # 24h 统计单独拉（币安不分在一个接口）
        try:
            stats = self._request("GET", "/fapi/v1/ticker/24hr",
                                 params={"symbol": symbol}, auth=False)
            high24 = float(stats.get("highPrice", 0))
            low24 = float(stats.get("lowPrice", 0))
            vol24 = float(stats.get("volume", 0))
        except Exception:  # noqa: BLE001
            high24 = low24 = vol24 = 0.0
        price = float(d["price"])
        return {
            "last": price, "bid": price, "ask": price,
            "high24h": high24, "low24h": low24, "vol24h": vol24,
            "ts": int(d.get("time", self._now_ms())),
        }

    def get_candles(
        self, inst_id: str, bar: str = "1H", limit: int = 300, **kw
    ) -> List[Dict[str, Any]]:
        """拉取K线，返回按时间升序的字典列表。"""
        symbol = self._to_binance_symbol(inst_id)
        interval = self._bar_to_interval(bar)
        resp = self._request("GET", "/fapi/v1/klines", params={
            "symbol": symbol, "interval": interval, "limit": str(min(limit, 1500)),
        }, auth=False)
        rows = []
        for raw in resp:
            rows.append({
                "ts": int(raw[0]),
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low": float(raw[3]),
                "close": float(raw[4]),
                "vol": float(raw[5]),
                "volCcy": float(raw[7]),     # quoteVolume
                "volQuote": float(raw[7]),
            })
        return rows  # 币安已升序

    def get_candles_paged(
        self, inst_id: str, bar: str, total: int = 1000
    ) -> List[Dict[str, Any]]:
        """分页拉取历史K线（币安用 startTime 翻页）。"""
        symbol = self._to_binance_symbol(inst_id)
        interval = self._bar_to_interval(bar)
        collected: List[Dict[str, Any]] = []
        start_time = None
        # 币安 klines 最多 1500 根/次；用 startTime 向前推进
        now_ms = self._now_ms()
        while len(collected) < total:
            params: Dict[str, Any] = {
                "symbol": symbol, "interval": interval, "limit": "1500",
            }
            if start_time:
                params["startTime"] = start_time
            resp = self._request("GET", "/fapi/v1/klines",
                                 params=params, auth=False)
            if not resp:
                break
            batch = []
            for raw in resp:
                row = {
                    "ts": int(raw[0]), "open": float(raw[1]),
                    "high": float(raw[2]), "low": float(raw[3]),
                    "close": float(raw[4]), "vol": float(raw[5]),
                    "volCcy": float(raw[7]), "volQuote": float(raw[7]),
                }
                batch.append(row)
            collected.extend(batch)
            if len(resp) < 1500:
                break
            start_time = int(resp[-1][0]) + 1
            if start_time > now_ms:
                break
        return collected[-total:]

    @staticmethod
    def _bar_to_interval(bar: str) -> str:
        """OKX bar → 币安 interval。"""
        m = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
             "1H": "1h", "2H": "2h", "4H": "4h", "6H": "6h", "12H": "12h",
             "1D": "1d", "1W": "1w"}
        if bar not in m:
            raise ValueError(f"不支持的K线周期: {bar}")
        return m[bar]

    # ==================================================================
    # 交易
    # ==================================================================
    def market_order(
        self, inst_id: str, side: str, sz: int,
        td_mode: str = "isolated", reduce_only: bool = False,
    ) -> str:
        """合约市价单。返回 orderId。

        sz 是 SwapTrader 内部整数张数；下单 quantity = sz * ctVal（ctVal=stepSize）。
        """
        symbol = self._to_binance_symbol(inst_id)
        inst = _BINANCE_SYMBOL_CACHE.get(symbol, {})
        ct_val = inst.get("binance_step_size", inst.get("ctVal", 1.0))
        qty = sz * ct_val
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),              # BUY/SELL
            "type": "MARKET",
            "quantity": self._fmt_qty_value(symbol, qty),
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        resp = self._request("POST", "/fapi/v1/order", params=params)
        return str(resp["orderId"])

    def place_oco(
        self, inst_id: str, side: str, sz: int,
        tp_trigger: float, sl_trigger: float, td_mode: str = "isolated",
    ) -> str:
        """下 OCO 止盈止损条件单。

        币安永续无原生 OCO，用 TAKE_PROFIT_MARKET + STOP_MARKET 两个 reduce-only
        条件单组合。algo_id 格式 "tp_id|sl_id"，对外透明。
        注：两腿互不连带，触发后另一腿需主动撤销（trader.maintain 平仓时调 cancel_algo）。
        """
        symbol = self._to_binance_symbol(inst_id)
        inst = _BINANCE_SYMBOL_CACHE.get(symbol, {})
        ct_val = inst.get("binance_step_size", inst.get("ctVal", 1.0))
        qty = sz * ct_val
        close_side = "SELL" if side.upper() == "SELL" else "BUY"
        qty_str = self._fmt_qty_value(symbol, qty)
        # 止盈腿
        tp_resp = self._request("POST", "/fapi/v1/order", params={
            "symbol": symbol, "side": close_side, "type": "TAKE_PROFIT_MARKET",
            "stopPrice": self._fmt_price(symbol, tp_trigger),
            "closePosition": "false",
            "quantity": qty_str, "reduceOnly": "true",
            "workingType": "MARK_PRICE",   # 用标记价触发，防插针
        })
        tp_id = str(tp_resp["orderId"])
        # 止损腿
        try:
            sl_resp = self._request("POST", "/fapi/v1/order", params={
                "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
                "stopPrice": self._fmt_price(symbol, sl_trigger),
                "closePosition": "false",
                "quantity": qty_str, "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            })
            sl_id = str(sl_resp["orderId"])
        except ExchangeApiError as e:
            # 止损腿失败必须清理止盈腿
            logger.critical("[币安] %s 止损腿下单失败(%s %s)，撤销已挂的止盈腿 %s",
                            inst_id, e.code, e.msg, tp_id)
            try:
                self._request("DELETE", "/fapi/v1/order",
                              params={"symbol": symbol, "orderId": tp_id})
            except Exception:  # noqa: BLE001
                pass
            raise
        logger.info("[币安] %s OCO 双腿已挂: tp=%s(%.2f) | sl=%s(%.2f)",
                    inst_id, tp_id, tp_trigger, sl_id, sl_trigger)
        return f"{tp_id}|{sl_id}"

    def get_pending_algos(self, inst_id: str) -> List[Dict[str, Any]]:
        """当前挂起的条件单（OCO 双腿都查）。"""
        symbol = self._to_binance_symbol(inst_id)
        resp = self._request("GET", "/fapi/v1/openOrder",
                             params={"symbol": symbol})  # 只返回当前 symbol 一单
        # 改用 openOrders 拉全部
        all_orders = self._request("GET", "/fapi/v1/openOrders",
                                   params={"symbol": symbol})
        out = []
        for o in all_orders:
            otype = o.get("type", "")
            if otype in ("TAKE_PROFIT_MARKET", "STOP_MARKET"):
                is_tp = otype == "TAKE_PROFIT_MARKET"
                out.append({
                    "algoId": f"{o['orderId']}|{o['orderId']}",  # 占位，trader 不用
                    "instId": inst_id,
                    "tpTriggerPx": float(o["stopPrice"]) if is_tp else 0.0,
                    "slTriggerPx": float(o["stopPrice"]) if not is_tp else 0.0,
                    "orderId": str(o["orderId"]),
                    "type": otype,
                })
        return out

    def cancel_algo(self, inst_id: str, algo_id: str) -> None:
        """撤销 OCO（同时撤 tp 与 sl 两腿）。"""
        symbol = self._to_binance_symbol(inst_id)
        for oid in algo_id.split("|"):
            if not oid:
                continue
            try:
                self._request("DELETE", "/fapi/v1/order",
                              params={"symbol": symbol, "orderId": oid})
            except ExchangeApiError as e:
                # -2011: 订单不存在/已成交/已撤，忽略
                if e.code != "-2011":
                    raise

    def amend_algo_sl(self, inst_id: str, algo_id: str, new_sl: float) -> bool:
        """修改止损触发价（追踪止损用）。

        币安不能直接改单，需撤掉止损腿重挂。algo_id="tp|sl"，只动 sl 那腿。
        返回新的 algo_id（成功）或 False（失败保留原止损）。
        但接口约定返回 bool——trader 内部用 amend 成功后 self.pos.algo_id 不变，
        我们用"撤旧 sl 重挂新 sl"的方式，新 id 替换回原 algo_id 的 sl 段。
        为保持接口一致，这里在客户端内部维护 id 映射。
        """
        symbol = self._to_binance_symbol(inst_id)
        parts = algo_id.split("|")
        if len(parts) != 2:
            logger.warning("[币安] amend_algo_sl: algo_id 格式异常 %s", algo_id)
            return False
        tp_id, sl_id = parts
        # 查原止损单的 side/qty
        try:
            old = self._request("GET", "/fapi/v1/order",
                                params={"symbol": symbol, "orderId": sl_id})
        except ExchangeApiError as e:
            if e.code == "-2013":
                # 订单不存在，按当前持仓重建
                return self._rebuild_sl(inst_id, tp_id, new_sl)
            logger.warning("[币安] 查询原止损单失败(%s %s)", e.code, e.msg)
            return False

        close_side = old.get("side", "SELL")
        qty = old.get("origQty", "")
        # 撤旧止损
        try:
            self._request("DELETE", "/fapi/v1/order",
                          params={"symbol": symbol, "orderId": sl_id})
        except ExchangeApiError as e:
            if e.code != "-2011":
                logger.warning("[币安] 撤旧止损失败(%s %s)", e.code, e.msg)
                return False
        # 挂新止损
        try:
            new_sl_resp = self._request("POST", "/fapi/v1/order", params={
                "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
                "stopPrice": self._fmt_price(symbol, new_sl),
                "quantity": qty, "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            })
            new_sl_id = str(new_sl_resp["orderId"])
            # 更新 trader 持有的 algo_id（通过写入映射，trader 不需感知）
            # 但 trader 直接用原 algo_id 调 amend，无法收到新 id——
            # 解决：客户端维护 old_algo_id → new_sl_id 的映射，下次 cancel/amend 用映射
            self._sl_id_map[algo_id] = new_sl_id
            logger.info("[币安] 止损价已更新 %.2f → %.2f (新 sl_id=%s)", old.get("stopPrice", 0), new_sl, new_sl_id)
            return True
        except ExchangeApiError as e:
            logger.warning("[币安] 挂新止损失败(%s %s)，原止损已撤！仓位暂时裸奔", e.code, e.msg)
            return False

    def _rebuild_sl(self, inst_id: str, tp_id: str, new_sl: float) -> bool:
        """原止损单已不存在（被触发/撤）→ 不应改，返回 False。"""
        logger.warning("[币安] %s 止损单已不存在，无法 amend", inst_id)
        return False

    @property
    def _sl_id_map(self) -> Dict[str, str]:
        """algo_id → 最新 sl_id 映射（amend 后 sl id 会变）。"""
        if not hasattr(self, "_sl_id_map_data"):
            self._sl_id_map_data: Dict[str, str] = {}
        return self._sl_id_map_data

    # ==================================================================
    # 订单生命周期
    # ==================================================================
    def wait_order_filled(self, inst_id: str, ord_id: str, **kw) -> Dict[str, Any]:
        """轮询等待订单成交。币安市价单通常立即成交。"""
        symbol = self._to_binance_symbol(inst_id)
        deadline = time.time() + kw.get("timeout", 20)
        poll = kw.get("poll", 1.0)
        while time.time() < deadline:
            info = self._get_order(symbol, ord_id)
            if info["state"] in ("filled", "canceled", "expired"):
                return info
            time.sleep(poll)
        return self._get_order(symbol, ord_id)

    def _get_order(self, symbol: str, ord_id: str) -> Dict[str, Any]:
        resp = self._request("GET", "/fapi/v1/order",
                             params={"symbol": symbol, "orderId": ord_id})
        st = resp.get("status", "NEW")
        state = {"NEW": "live", "PARTIALLY_FILLED": "partially_filled",
                 "FILLED": "filled", "CANCELED": "canceled",
                 "EXPIRED": "expired", "REJECTED": "canceled"}.get(st, st)
        return {
            "ordId": str(resp["orderId"]),
            "state": state,
            "avgPx": float(resp.get("avgPrice") or 0),
            "accFillSz": float(resp.get("executedQty") or 0),
            "fillPx": float(resp.get("avgPrice") or 0),
            "fillSz": float(resp.get("executedQty") or 0),
            "fee": float(resp.get("commission") or 0),
            "pnl": float(resp.get("cumQuote", 0)) or 0.0,
            "side": resp.get("side", "").lower(),
        }

    def cancel_order(self, inst_id: str, ord_id: str) -> None:
        symbol = self._to_binance_symbol(inst_id)
        try:
            self._request("DELETE", "/fapi/v1/order",
                          params={"symbol": symbol, "orderId": ord_id})
        except ExchangeApiError as e:
            if e.code != "-2011":  # 订单不存在
                raise

    # ==================================================================
    # 持仓 / 账户
    # ==================================================================
    def get_position(self, inst_id: str) -> Optional[Dict[str, Any]]:
        """查询某合约当前持仓。无持仓返回 None。

        币安 positionAmt 是币数（如 0.015 BTC）；换算为内部整数张数 = amt / ctVal。
        返回的 contracts 带方向：正多负空。
        """
        symbol = self._to_binance_symbol(inst_id)
        inst = _BINANCE_SYMBOL_CACHE.get(symbol, {})
        ct_val = inst.get("binance_step_size", inst.get("ctVal", 1.0))
        resp = self._request("GET", "/fapi/v2/positionRisk", params={"symbol": symbol})
        for p in resp:
            if p["symbol"] == symbol:
                amt = float(p.get("positionAmt") or 0)
                if abs(amt) > 1e-9:
                    contracts = amt / ct_val if ct_val > 0 else amt
                    return {
                        "instId": inst_id,
                        "contracts": contracts,               # 内部张数（带方向）
                        "avgPx": float(p.get("entryPrice") or 0),
                        "upl": float(p.get("unRealizedProfit") or 0),
                        "lever": p.get("leverage", ""),
                        "margin": float(p.get("isolatedMargin") or 0),
                        "liqPx": float(p.get("liquidationPrice") or 0),
                    }
        return None

    def get_equity(self, ccy: str = "USDT") -> float:
        """账户权益（USDT 计）。币安用账户余额里的 availableBalance + 保证金。"""
        resp = self._request("GET", "/fapi/v2/balance")
        for a in resp:
            if a.get("asset") == ccy:
                # balance = 钱包余额；可用 = availableBalance；权益 ≈ balance（含浮盈）
                return float(a.get("balance") or 0)
        return 0.0

    def get_positions_history(self, inst_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """最近平仓历史（按时间倒序），用于连续亏损统计。

        币安用 income（realizedPnl=类型为 REALIZED_PNL 的收入）。
        """
        symbol = self._to_binance_symbol(inst_id)
        resp = self._request("GET", "/fapi/v1/income", params={
            "symbol": symbol, "incomeType": "REALIZED_PNL",
            "limit": str(min(limit, 1000)),
        })
        out = []
        for h in resp:
            pnl = float(h.get("income") or 0)
            # 币安没有 direction 字段，用 symbol 的时间戳聚合近似
            out.append({
                "instId": inst_id,
                "direction": "unknown",     # 币安 income 不带方向，trader 不依赖此字段
                "realizedPnl": pnl,
                "posId": str(h.get("tranId", "")),
                "uTime": int(h.get("time") or 0),
                "type": 2,
            })
        # 按时间倒序
        out.sort(key=lambda x: x["uTime"], reverse=True)
        return out

    # ==================================================================
    # 工具
    # ==================================================================
    def _fmt_qty_value(self, symbol: str, qty: float) -> str:
        """按合约 stepSize 格式化币数量。"""
        inst = _BINANCE_SYMBOL_CACHE.get(symbol)
        step = inst.get("binance_step_size", inst.get("ctVal", 1.0)) if inst else 1.0
        if step > 0:
            import math
            q = math.floor(qty / step + 1e-9) * step
            step_str = f"{step:.10f}".rstrip("0").rstrip(".")
            precision = len(step_str.split(".")[-1]) if "." in step_str else 0
            return f"{q:.{precision}f}"
        return str(qty)

    def _fmt_price(self, symbol: str, price: float) -> str:
        """按 tickSz 格式化价格。"""
        inst = _BINANCE_SYMBOL_CACHE.get(symbol)
        if inst and inst["tickSz"] > 0:
            import math
            n = round(price / inst["tickSz"])
            p = n * inst["tickSz"]
            precision = len(str(inst["tickSz"]).rstrip("0").split(".")[-1]) if "." in str(inst["tickSz"]) else 0
            return f"{p:.{precision}f}"
        return str(price)
