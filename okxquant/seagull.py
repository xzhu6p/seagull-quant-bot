"""
Seagull 自适应策略 —— MT5 EA (Seagull_Adaptive_Pro.mq5) 的忠实转化。

原 EA 交易 XAUUSD，本版本适配 OKX USDT 永续合约（BTC/ETH 等），
逻辑逐条对应，参数全部开放：

┌─────────────────── EA 原逻辑 ───────────────────┬──────────── 本实现 ────────────┐
│ 新K线出现时评估上一根已收盘K线(shift=1)          │ 引擎只在K线收盘后调用 on_bar   │
│ close[1] > EMA50 趋势过滤                        │ df.close.iloc[-1] > ema50      │
│ close[1]>open[1] 阳线 + 实体占比≥ InpMinBodyRatio│ body/range ≥ min_body_ratio    │
│ MACD(12,26,9) 主线 > 信号线                      │ dif > dea                     │
│ 多单: SL=ask-1.5×ATR, TP=ask+2×ATR              │ 对应 atr_sl/atr_tp 系数        │
│ 空单(对称): SL=bid+1.5×ATR, TP=bid-2×ATR        │ 同上镜像                      │
│ 连续亏损≥2 → 手数降至 0.01                       │ 名义价值降至降仓档(最小张数)    │
│ 日内回撤≥ $45 → 当日停止开仓                     │ 引擎 equity 熔断               │
│ PositionsTotal() ≥ 3 不开新仓                    │ max_open_instruments 限制      │
│ ApplyFastTrailing 微型追踪锁利                   │ trailing_start/dist_atr 参数化 │
└─────────────────────────────────────────────────┴───────────────────────────────┘

注意：原 EA 中空单分支与追踪止损实现未在贴出的代码片段中给出，
此处按多单逻辑对称补全 + 标准 ATR 追踪实现，参数可调。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pandas as pd

from .indicators import atr, ema, macd

logger = logging.getLogger(__name__)


@dataclass
class SeagullSignal:
    """开仓信号（含该仓的止损/止盈价）。"""
    direction: str          # "long" / "short"
    entry_ref: float        # 信号参考价（K线收盘价；实际入场用市价）
    sl_price: float         # 止损触发价
    tp_price: float         # 止盈触发价
    atr: float              # 信号时的 ATR（追踪止损继续沿用）
    reason: str             # 信号描述


@dataclass
class SeagullParams:
    # ── 动能与趋势引擎（对应 EA input）──
    fast_ema: int = 50           # InpFastEmaPeriod: 50 均线趋势过滤
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    min_body_ratio: float = 0.45  # InpMinBodyRatio: K线实体动能占比
    atr_period: int = 14          # InpAtrPeriod
    # ── ATR 止损止盈 ──
    sl_atr: float = 1.5           # 1.5×ATR 硬止损
    tp_atr: float = 2.0           # 2.0×ATR 动态止盈
    # ── 微型追踪锁利（EA 原文截断，按标准实现参数化）──
    use_trailing: bool = True     # InpUseTrailing
    trailing_start_atr: float = 1.0   # 浮盈达 1.0×ATR 后启动追踪
    trailing_dist_atr: float = 0.8    # 追踪距离 0.8×ATR，且不低于保本价
    # ── 仓位与风控 ──
    notional_per_order: float = 100.0     # 每单名义价值 USDT（代替 EA 手数）
    reduced_notional: float = 0.0         # 连亏降仓档名义价值；0=用最小张数
    max_open_instruments: int = 3         # InpPositionsTotal>=3: 最多同时持仓的币种数
    max_daily_loss_usd: float = 45.0      # InpMaxDailyLossUSD 日内熔断
    losing_streak_to_reduce: int = 2      # 连亏≥2 次降仓（EA 硬编码 2）

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SeagullParams":
        valid = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in valid})


class SeagullStrategy:
    """信号生成 + 追踪止损计算（纯逻辑，不碰任何 I/O，实盘回测共用）。"""

    name = "seagull_adaptive_pro"

    def __init__(self, params: Dict[str, Any]) -> None:
        self.p = SeagullParams.from_dict(params or {})
        # 连续亏损计数（引擎/回测在平仓后更新；对应 EA 全局 consecutiveLosses）
        self.losing_streak: int = 0

    # ------------------------------------------------------------------
    @property
    def warmup_bars(self) -> int:
        return max(self.p.fast_ema, self.p.macd_slow) + self.p.atr_period + 10

    def current_notional(self) -> float:
        """当前应使用的名义价值（连亏≥N 次自动降仓，对应 EA 动态降仓）。"""
        if self.losing_streak >= self.p.losing_streak_to_reduce:
            return self.p.reduced_notional  # 0 由 trader 解释为"最小张数"
        return self.p.notional_per_order

    # ------------------------------------------------------------------
    def on_bar(self, df: pd.DataFrame) -> Optional[SeagullSignal]:
        """新K线收盘时评估开仓信号（无信号返回 None）。

        df 为升序K线，最后一根 = EA 里的 shift=1（已收盘K线）。
        """
        p = self.p
        if len(df) < self.warmup_bars:
            return None

        close = df["close"]
        open_ = df["open"]
        high = df["high"]
        low = df["low"]

        c1, o1 = float(close.iloc[-1]), float(open_.iloc[-1])
        h1, l1 = float(high.iloc[-1]), float(low.iloc[-1])

        # 指标（取最后一根已收盘K线对应值）
        ema_line = ema(close, p.fast_ema)
        dif, dea, _ = macd(close, p.macd_fast, p.macd_slow, p.macd_signal)
        atr_line = atr(df, p.atr_period)
        ema_now = float(ema_line.iloc[-1])
        dif_now, dea_now = float(dif.iloc[-1]), float(dea.iloc[-1])
        atr_now = float(atr_line.iloc[-1])
        if any(pd.isna(x) for x in (ema_now, dif_now, dea_now, atr_now)) or atr_now <= 0:
            return None

        # K线实体动能占比（对应 EA hasStrongBody）
        total_range = h1 - l1
        if total_range <= 0:
            return None
        body_ratio = abs(c1 - o1) / total_range
        has_strong_body = body_ratio >= p.min_body_ratio

        # ── 多单：收盘在 EMA50 上方 + 阳线 + 强实体 + MACD 金叉上方 ──
        if c1 > ema_now and c1 > o1 and has_strong_body and dif_now > dea_now:
            signal = SeagullSignal(
                direction="long",
                entry_ref=c1,
                sl_price=c1 - p.sl_atr * atr_now,
                tp_price=c1 + p.tp_atr * atr_now,
                atr=atr_now,
                reason=(
                    f"多信号: close {c1:.1f}>EMA{p.fast_ema} {ema_now:.1f}, "
                    f"阳线实体{body_ratio:.0%}, MACD dif>dea"
                ),
            )
            logger.info("[Seagull] %s | SL=%.2f TP=%.2f ATR=%.2f",
                        signal.reason, signal.sl_price, signal.tp_price, atr_now)
            return signal

        # ── 空单（对称）：收盘在 EMA50 下方 + 阴线 + 强实体 + MACD 死叉下方 ──
        if c1 < ema_now and c1 < o1 and has_strong_body and dif_now < dea_now:
            signal = SeagullSignal(
                direction="short",
                entry_ref=c1,
                sl_price=c1 + p.sl_atr * atr_now,
                tp_price=c1 - p.tp_atr * atr_now,
                atr=atr_now,
                reason=(
                    f"空信号: close {c1:.1f}<EMA{p.fast_ema} {ema_now:.1f}, "
                    f"阴线实体{body_ratio:.0%}, MACD dif<dea"
                ),
            )
            logger.info("[Seagull] %s | SL=%.2f TP=%.2f ATR=%.2f",
                        signal.reason, signal.sl_price, signal.tp_price, atr_now)
            return signal

        return None

    # ------------------------------------------------------------------
    def trailing_stop(
        self,
        direction: str,
        entry_price: float,
        current_price: float,
        current_sl: float,
        atr_value: float,
    ) -> Optional[float]:
        """微型追踪锁利（对应 EA ApplyFastTrailing，原文截断按标准实现）。

        返回新的止损价；不应移动时返回 None。
        规则：浮盈 ≥ trailing_start_atr×ATR 启动；止损跟随价格保持
        trailing_dist_atr×ATR 距离，且永不低于保本价（多仓）/不高于保本价（空仓），
        只朝有利方向移动。
        """
        p = self.p
        if not p.use_trailing or atr_value <= 0:
            return None

        if direction == "long":
            profit = current_price - entry_price
            if profit < p.trailing_start_atr * atr_value:
                return None
            candidate = current_price - p.trailing_dist_atr * atr_value
            candidate = max(candidate, entry_price)          # 至少保本
            if current_sl <= 0 or candidate > current_sl + 1e-9:
                return candidate
        else:
            profit = entry_price - current_price
            if profit < p.trailing_start_atr * atr_value:
                return None
            candidate = current_price + p.trailing_dist_atr * atr_value
            candidate = min(candidate, entry_price)          # 至少保本
            if current_sl <= 0 or candidate < current_sl - 1e-9:
                return candidate
        return None
