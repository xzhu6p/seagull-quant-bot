"""
Seagull 引擎：多币种轮询调度（对应 EA 的 OnTick 循环）。

职责映射：
  OnTick 的新K线判断(lastBarTime)  →  last_bar_ts 去重（只处理已收盘K线）
  CheckDailyCircuitBreaker         →  _check_daily_breaker（equity 回撤熔断）
  PositionsTotal() >= 3            →  已开仓币种数 >= max_open_instruments
  ApplyFastTrailing                →  trader.maintain()（每周期追踪止损）

三种运行模式（按 config.exchange 路由不同客户端）：
  okx    OkxSwapClient    —— OKX 永续合约（默认 sim=true 模拟盘！）
  binance BinanceSwapClient —— 币安 USDT 永续（testnet=true 模拟盘）
  paper  PaperSwapClient  —— 本地撮合，无需 API Key 验证全链路
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from .base_client import SwapClientBase
from .binance_swap_client import BinanceSwapClient
from .paper import PaperSwapClient, SimulatedFeed, bar_seconds
from .seagull import SeagullSignal, SeagullStrategy
from .swap_client import OkxSwapClient
from .swap_trader import SwapTrader

logger = logging.getLogger(__name__)


def build_swap_client(cfg: Dict[str, Any]) -> SwapClientBase:
    """按 config.exchange 实例化对应交易所客户端。"""
    exchange = str(cfg.get("exchange", "okx")).lower()
    if exchange == "okx":
        return OkxSwapClient(
            api_key=cfg.get("api_key", ""),
            secret_key=cfg.get("secret_key", ""),
            passphrase=cfg.get("passphrase", ""),
            simulated=bool(cfg.get("simulated", True)),
        )
    if exchange == "binance":
        return BinanceSwapClient(
            api_key=cfg.get("api_key", ""),
            secret_key=cfg.get("secret_key", ""),
            testnet=bool(cfg.get("testnet", True)),
        )
    raise ValueError(f"不支持的交易所: {exchange}（支持 okx / binance / paper）")


class SeagullEngine:
    """OKX 永续合约 Seagull 策略引擎。"""

    def __init__(
        self,
        cfg: Dict[str, Any],
        paper: bool = False,
        feed: Optional[Any] = None,
        state_file: str = "state.json",
    ) -> None:
        self.cfg = cfg
        self.paper = paper
        self.bar: str = cfg.get("bar", "15m")
        self.bar_sec = bar_seconds(self.bar)
        self.poll_interval: int = int(cfg.get("poll_interval", 30))
        strategy_params: Dict[str, Any] = cfg.get("strategy", {})
        self.max_open = int(strategy_params.get("max_open_instruments", 3))
        self.max_daily_loss = float(strategy_params.get("max_daily_loss_usd", 45.0))
        self.td_mode: str = cfg.get("risk", {}).get("td_mode", "isolated")

        inst_ids: List[str] = cfg.get("inst_ids", ["BTC-USDT-SWAP"])
        if isinstance(inst_ids, str):
            inst_ids = [i.strip() for i in inst_ids.split(",") if i.strip()]

        # ── 客户端（按 exchange 路由）──
        if paper:
            self.client = PaperSwapClient(
                initial_equity=float(cfg.get("paper_equity", 10_000.0))
            )
            self.feed = feed or SimulatedFeed(inst_ids, self.bar)
        else:
            self.client = build_swap_client(cfg)
            self.feed = self.client
            self.client.set_position_mode_net()
            lever = int(cfg.get("risk", {}).get("leverage", 3))
            for iid in inst_ids:
                try:
                    self.client.set_leverage(iid, lever, self.td_mode)
                except Exception as e:  # noqa: BLE001
                    logger.warning("设置 %s 杠杆失败: %s", iid, e)

        # ── 每币种：策略 + 执行器 ──
        self.traders: Dict[str, SwapTrader] = {}
        self.strategies: Dict[str, SeagullStrategy] = {}
        for iid in inst_ids:
            try:
                inst_meta = self.client.get_instrument(iid)
            except Exception as e:  # noqa: BLE001
                if paper:
                    raise
                logger.error("获取合约规则失败 %s: %s，跳过该币种", iid, e)
                continue
            self.strategies[iid] = SeagullStrategy(strategy_params)
            self.traders[iid] = SwapTrader(
                self.client, self.strategies[iid], inst_meta, td_mode=self.td_mode
            )

        self.state_file = state_file
        self._last_bar_ts: Dict[str, int] = {}
        self._day: Optional[date] = None
        self._day_start_equity = 0.0
        self._day_locked = False
        self._load_state()

    # ==================================================================
    # 主循环
    # ==================================================================
    def run(self) -> None:
        exchange = self.cfg.get("exchange", "okx").upper()
        if self.paper:
            mode = "纸面模式(本地撮合)"
        elif exchange == "BINANCE":
            mode = "币安测试网(Binance Testnet)" if self.cfg.get("testnet", True) else "币安实盘!"
        else:
            mode = "OKX 模拟盘(Demo Trading)" if self.cfg.get("simulated", True) else "OKX 实盘!"
        logger.info("=" * 62)
        logger.info("Seagull 自适应策略引擎启动 [%s]", mode)
        logger.info("合约: %s | 周期: %s | 轮询: %ds", list(self.traders), self.bar, self.poll_interval)
        logger.info("熔断: 日内回撤 $%.2f | 单币名义: $%.2f | 最多同时持仓币种: %d",
                    self.max_daily_loss,
                    self.cfg.get("strategy", {}).get("notional_per_order", 100),
                    self.max_open)
        logger.info("=" * 62)
        try:
            while True:
                started = time.time()
                try:
                    self._tick()
                except KeyboardInterrupt:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("本周期执行异常，继续运行")
                elapsed = time.time() - started
                time.sleep(max(1.0, self.poll_interval - elapsed))
        except KeyboardInterrupt:
            logger.info("收到退出信号，保存状态后退出（持仓与交易所侧止损保留）")
            self._save_state()

    def _tick(self) -> None:
        # 1) 日内熔断（对应 CheckDailyCircuitBreaker）
        self._check_daily_breaker()

        # 2) 持仓维护：追踪止损 + 平仓检测（对应 ApplyFastTrailing）
        for iid, trader in self.traders.items():
            trader.maintain()

        # 3) K线驱动信号评估（对应 OnTick 新K线 + CheckAndTradeAdaptive）
        for iid, trader in self.traders.items():
            df = self._fetch_closed_candles(iid)
            if df is None or df.empty:
                continue
            strategy = self.strategies[iid]
            if len(df) < strategy.warmup_bars:
                continue
            signal = strategy.on_bar(df)
            if signal is None:
                continue
            self._maybe_open(iid, trader, signal)

        # 4) 持久化
        self._save_state()

    # ==================================================================
    def _maybe_open(self, inst_id: str, trader: SwapTrader, signal: SeagullSignal) -> None:
        """开仓闸门：熔断 / 已有持仓 / 持仓币种上限（对应 EA 的三重限制）。"""
        if self._day_locked:
            logger.info("[拦截] %s 信号被日内熔断拦截: %s", inst_id, signal.reason)
            return
        if trader.pos.is_open:
            return  # 该币种已有持仓
        open_count = sum(1 for t in self.traders.values() if t.pos.is_open)
        if open_count >= self.max_open:
            logger.info("[拦截] %s 信号被持仓上限拦截(已持仓 %d 个币种)", inst_id, open_count)
            return
        if trader.open_position(signal) and self.paper:
            self.client.annotate_open(inst_id, signal.reason)  # type: ignore[attr-defined]

    def _check_daily_breaker(self) -> None:
        today = date.today()
        if self._day != today:
            self._day = today
            self._day_start_equity = self.client.get_equity()
            self._day_locked = False
            logger.info("[熔断器] 新交易日，权益基准 $%.2f", self._day_start_equity)
            return
        equity = self.client.get_equity()
        drawdown = self._day_start_equity - equity
        if drawdown >= self.max_daily_loss and not self._day_locked:
            self._day_locked = True
            logger.error(
                "[熔断器] 日内回撤 $%.2f ≥ 上限 $%.2f，今日停止开仓！（持仓止损仍在生效）",
                drawdown, self.max_daily_loss,
            )

    # ==================================================================
    # K线获取：仅保留已收盘K线
    # ==================================================================
    def _fetch_closed_candles(self, inst_id: str) -> Optional[pd.DataFrame]:
        try:
            rows = self.feed.get_candles(inst_id, self.bar, limit=400)
        except Exception as e:  # noqa: BLE001
            logger.warning("拉取 %s K线失败: %s", inst_id, e)
            return None
        if not rows:
            return None

        now_ms = int(time.time() * 1000)
        closed = [r for r in rows if r["ts"] + self.bar_sec * 1000 <= now_ms]
        # 纸面/回测模式时间可能超前，退化为使用全部（模拟推进本身只产生已收盘K线）
        if not closed:
            closed = rows[:-1] if len(rows) > 1 else rows
        if not closed:
            return None

        last_ts = closed[-1]["ts"]
        if self._last_bar_ts.get(inst_id) == last_ts:
            return None  # 无新K线
        self._last_bar_ts[inst_id] = last_ts

        # 纸面模式：每根新K线先做 SL/TP 触发检查并刷新最新价
        if self.paper:
            paper = self.client  # type: PaperSwapClient
            paper.on_bar(inst_id, closed[-1])
            paper.set_price(inst_id, closed[-1]["close"])

        df = pd.DataFrame(closed)
        return df[["ts", "open", "high", "low", "close", "vol"]]

    # ==================================================================
    # 状态持久化
    # ==================================================================
    def _save_state(self) -> None:
        data = {
            "positions": {iid: t.state_dict() for iid, t in self.traders.items()},
            "losing_streak": {iid: s.losing_streak for iid, s in self.strategies.items()},
            "last_bar_ts": self._last_bar_ts,
            "day": str(self._day or ""),
            "day_start_equity": self._day_start_equity,
        }
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.warning("状态保存失败: %s", e)

    def _load_state(self) -> None:
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for iid, d in data.get("positions", {}).items():
                if iid in self.traders:
                    self.traders[iid].load_state(d)
            for iid, n in data.get("losing_streak", {}).items():
                if iid in self.strategies:
                    self.strategies[iid].losing_streak = int(n)
            self._last_bar_ts = data.get("last_bar_ts", {})
            self._day = date.fromisoformat(data["day"]) if data.get("day") else None
            self._day_start_equity = float(data.get("day_start_equity", 0))
            logger.info("已恢复状态文件 %s", self.state_file)
        except Exception as e:  # noqa: BLE001
            logger.warning("状态恢复失败(%s)，从零开始", e)

        # 本地无仓但交易所有仓（实盘重启场景）→ 从交易所重建镜像
        if not self.paper:
            for iid, trader in self.traders.items():
                trader.restore_from_exchange()
