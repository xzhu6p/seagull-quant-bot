"""
OKX V5 WebSocket 行情订阅（公共频道）。

- 自动重连 + 30 秒心跳（发送文本 "ping"）
- 订阅 tickers / candle 频道，回调推送数据
- 依赖: pip install websocket-client
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

WS_PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"
PONG_TEXT = "pong"


class OkxWebSocket:
    """OKX 公共行情 WebSocket 客户端（后台线程运行）。"""

    def __init__(
        self,
        url: str = WS_PUBLIC_URL,
        on_ticker: Optional[Callable[[dict], None]] = None,
        on_candle: Optional[Callable[[dict], None]] = None,
        simulated: bool = False,
    ) -> None:
        self.url = url
        if simulated:
            self.url = "wss://wspap.okx.com:8443/ws/v5/public"  # 模拟盘行情
        self.on_ticker = on_ticker
        self.on_candle = on_candle
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="okx-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def subscribe(self, inst_id: str, bars: Optional[list] = None) -> None:
        """订阅实例的 ticker 与可选K线频道（如 ["1m", "1H"]）。"""
        self._inst_id = inst_id
        self._bars = bars or []

    def _run(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            logger.error("缺少 websocket-client，请先安装: pip install websocket-client")
            return

        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(self.url, timeout=10)
                args = [{"channel": "tickers", "instId": self._inst_id}]
                for bar in self._bars:
                    args.append({"channel": f"candle{bar}", "instId": self._inst_id})
                ws.send(json.dumps({"op": "subscribe", "args": args}))
                logger.info("WebSocket 已连接并订阅: %s", self._inst_id)
                self._loop(ws)
            except Exception as e:  # noqa: BLE001
                logger.warning("WebSocket 断开(%s)，5 秒后重连", e)
                self._stop.wait(5)

    def _loop(self, ws) -> None:
        last_ping = time.time()
        ws.settimeout(5)
        while not self._stop.is_set():
            if time.time() - last_ping > 20:  # 空闲30秒会被服务端断开，20秒发一次心跳
                ws.send("ping")
                last_ping = time.time()
            try:
                raw = ws.recv()
            except Exception:  # 超时继续检查心跳
                continue
            if raw == PONG_TEXT:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("event") in ("subscribe", "error"):
                if msg.get("event") == "error":
                    logger.error("WebSocket 订阅失败: %s", msg)
                continue
            channel = msg.get("arg", {}).get("channel", "")
            data = msg.get("data") or []
            if not data:
                continue
            if channel == "tickers" and self.on_ticker:
                d = data[0]
                self.on_ticker(
                    {
                        "last": float(d["last"]),
                        "bid": float(d.get("bidPx") or 0),
                        "ask": float(d.get("askPx") or 0),
                        "ts": int(d["ts"]),
                    }
                )
            elif channel.startswith("candle") and self.on_candle:
                d = data[0]
                # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
                self.on_candle(
                    {
                        "ts": int(d[0]),
                        "open": float(d[1]),
                        "high": float(d[2]),
                        "low": float(d[3]),
                        "close": float(d[4]),
                        "vol": float(d[5]),
                        "confirm": bool(int(d[8])) if len(d) > 8 else False,
                    }
                )
