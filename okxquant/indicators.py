"""常用技术指标（基于 pandas，输入为K线 DataFrame）。"""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """简单移动平均。"""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均。"""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """相对强弱指标（Wilder 平滑）。"""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    result = 100.0 - 100.0 / (1.0 + rs)
    return result.astype(float).fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """平均真实波幅。"""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0):
    """布林带，返回 (mid, upper, lower)。"""
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std(ddof=0)
    return mid, mid + num_std * std, mid - num_std * std


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD，返回 (dif, dea, hist)。

    dif  = EMA(fast) - EMA(slow)   （MT5 MACD 主线）
    dea  = EMA(dif, signal)        （MT5 MACD 信号线）
    hist = dif - dea
    """
    dif = ema(series, fast) - ema(series, slow)
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, dif - dea
