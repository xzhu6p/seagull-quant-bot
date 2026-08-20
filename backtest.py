"""
Seagull 策略回测引擎。

核心原则：回测与实盘共用 SeagullStrategy + SwapTrader + PaperSwapClient，
**回测验证的就是实盘执行的代码路径**（信号 → 闸门 → 撮合 → SL/TP → 追踪）。

用法：
  python backtest.py                          # 模拟行情（BTC+ETH，15m，约45天）
  python backtest.py --insts BTC-USDT-SWAP --bars 5000
  python backtest.py --csv data/BTC_15m.csv --insts BTC-USDT-SWAP
  python backtest.py --config config.json     # 使用与实盘一致的参数回测

CSV 格式: ts(秒或毫秒),open,high,low,close,vol
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

from okxquant.engine import SeagullEngine  # noqa: F401  (确保模块可用)
from okxquant.paper import PaperSwapClient, SimulatedFeed, bar_seconds
from okxquant.seagull import SeagullStrategy
from okxquant.swap_trader import SwapTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtest")


# ----------------------------------------------------------------------
def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    need = {"ts", "open", "high", "low", "close"}
    if not need.issubset(df.columns):
        raise ValueError(f"CSV 缺少列 {need - set(df.columns)}，实际列: {list(df.columns)}")
    if df["ts"].iloc[0] < 1e12:  # 秒 → 毫秒
        df["ts"] = df["ts"] * 1000
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def run_backtest(
    cfg: Dict[str, Any],
    df_map: Dict[str, pd.DataFrame],
    initial_equity: float = 10_000.0,
    fee_rate: float = 0.0005,
    slippage_ticks: int = 1,
) -> Dict[str, Any]:
    """跑回测，返回 {paper, equity_curve, trades, metrics}。"""
    strategy_params = cfg.get("strategy", {})
    inst_ids = list(df_map.keys())
    td_mode = cfg.get("risk", {}).get("td_mode", "isolated")
    max_open = int(strategy_params.get("max_open_instruments", 3))
    max_daily_loss = float(strategy_params.get("max_daily_loss_usd", 45.0))

    paper = PaperSwapClient(initial_equity=initial_equity,
                            fee_rate=fee_rate, slippage_ticks=slippage_ticks)
    strategies: Dict[str, SeagullStrategy] = {}
    traders: Dict[str, SwapTrader] = {}
    for iid in inst_ids:
        inst_meta = paper.get_instrument(iid)
        strategies[iid] = SeagullStrategy(strategy_params)
        traders[iid] = SwapTrader(paper, strategies[iid], inst_meta, td_mode=td_mode)

    warmup = max(s.warmup_bars for s in strategies.values())
    n_bars = max(len(df) for df in df_map.values())

    # 日内熔断状态（按K线模拟日期）
    day_key = None
    day_start_equity = initial_equity
    day_locked = False

    equity_curve: List[float] = []
    timestamps: List[int] = []
    stop_at = n_bars

    for i in range(n_bars):
        # 每根K线按"时间最早推进"的原则处理各币种
        for iid, df in df_map.items():
            if i >= len(df):
                continue
            row = df.iloc[i]
            paper.on_bar(iid, row.to_dict())             # 1) SL/TP 触发（悲观先止损）
            paper.set_price(iid, float(row["close"]))    # 2) 刷新最新价
            traders[iid].maintain()                      # 3) 追踪止损（交易所侧）

        # 4) 日内熔断（按模拟日期）
        ref_ts = int(df_map[inst_ids[0]].iloc[min(i, len(df_map[inst_ids[0]]) - 1)]["ts"])
        cur_day = datetime.fromtimestamp(ref_ts / 1000, tz=timezone.utc).date()
        if cur_day != day_key:
            day_key = cur_day
            day_start_equity = paper.get_equity()
            day_locked = False
        if not day_locked and day_start_equity - paper.get_equity() >= max_daily_loss:
            day_locked = True
            logger.warning("[回测-熔断] %s 日内回撤达 $%.2f，该日停止开仓",
                           cur_day, day_start_equity - paper.get_equity())

        # 5) 信号评估（用截至当前K线的已收盘数据）
        for iid, df in df_map.items():
            if i + 1 < warmup or i + 1 > len(df):
                continue
            window = df.iloc[: i + 1]
            signal = strategies[iid].on_bar(window)
            if signal is None:
                continue
            if day_locked or traders[iid].pos.is_open:
                continue
            open_count = sum(1 for t in traders.values() if t.pos.is_open)
            if open_count >= max_open:
                continue
            if traders[iid].open_position(signal):
                paper.annotate_open(iid, signal.reason)

        equity_curve.append(paper.get_equity())
        timestamps.append(ref_ts)

        if len(equity_curve) % 2000 == 0:
            logger.info("回测进度 %d/%d bars, equity=%.2f", i + 1, n_bars, equity_curve[-1])
        stop_at = i + 1

    # 收尾：未平仓位按最后价格强平统计
    for iid, trader in traders.items():
        if trader.pos.is_open:
            df = df_map[iid]
            last = df.iloc[-1]
            paper.set_price(iid, float(last["close"]))
            trader.close_all()

    trades = paper.trade_history
    metrics = compute_metrics(equity_curve, timestamps, trades,
                              bar_seconds(cfg.get("bar", "15m")), initial_equity)
    return {"paper": paper, "equity_curve": equity_curve, "timestamps": timestamps,
            "trades": trades, "metrics": metrics, "stop_at": stop_at}


# ----------------------------------------------------------------------
def compute_metrics(equity: List[float], timestamps: List[int], trades: List[dict],
                    bar_sec: int, initial: float) -> Dict[str, Any]:
    if not equity:
        return {}
    eq = pd.Series(equity)
    final = float(eq.iloc[-1])
    total_ret = final / initial - 1

    bars_per_year = 365 * 24 * 3600 / bar_sec
    n = len(eq)
    years = n / bars_per_year if bars_per_year else 0
    annual_ret = (1 + total_ret) ** (1 / years) - 1 if years > 0 and total_ret > -1 else 0.0

    roll_max = eq.cummax()
    dd = (eq - roll_max) / roll_max
    max_dd = float(dd.min()) if len(dd) else 0.0

    rets = eq.pct_change().dropna()
    sharpe = 0.0
    if len(rets) > 2 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * (bars_per_year ** 0.5))

    wins = [t for t in trades if t["realizedPnl"] > 0]
    losses = [t for t in trades if t["realizedPnl"] <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_win = sum(t["realizedPnl"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t["realizedPnl"] for t in losses) / len(losses)) if losses else 0.0
    profit_factor = (avg_win * len(wins)) / (avg_loss * len(losses)) \
        if losses and avg_loss > 0 else float("inf")

    # 最大连亏次数
    max_streak = streak = 0
    for t in trades:
        streak = streak + 1 if t["realizedPnl"] <= 0 else 0
        max_streak = max(max_streak, streak)

    return {
        "initial": initial,
        "final": final,
        "total_return": total_ret,
        "annual_return": annual_ret,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "n_trades": len(trades),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_losing_streak": max_streak,
        "total_fees": sum(t.get("fees", 0) for t in trades),
        "bars": n,
    }


# ----------------------------------------------------------------------
def plot_result(res: Dict[str, Any], df_map: Dict[str, pd.DataFrame],
                out_png: str, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    trades = res["trades"]
    eq = res["equity_curve"]
    ts = [t / 1000 for t in res["timestamps"]]
    inst_ids = list(df_map.keys())
    n = len(inst_ids)
    fig, axes = plt.subplots(n + 1, 1, figsize=(14, 4.2 * (n + 1)), sharex=False,
                             gridspec_kw={"height_ratios": [3] * n + [2]})
    if n == 0:
        return
    axes = list(axes)

    colors = {"long": "#e0562c", "short": "#1f77b4"}
    for ax, iid in zip(axes, inst_ids):
        df = df_map[iid]
        t = df["ts"] / 1000
        ax.plot(t, df["close"], lw=0.8, color="#888", label=f"{iid} close")
        first_long = first_short = True
        for tr in [x for x in trades if x["instId"] == iid]:
            c = colors.get(tr["direction"], "k")
            t0 = tr["opened_at"] / 1000
            t1 = tr["uTime"] / 1000
            if tr["direction"] == "long":
                lbl = "long entry" if first_long else None
                first_long = False
            else:
                lbl = "short entry" if first_short else None
                first_short = False
            ax.scatter([t0], [tr["entry_price"]], marker="^" if tr["direction"] == "long" else "v",
                       color=c, s=42, zorder=5, label=lbl)
            ax.scatter([t1], [tr["exit_price"]], marker="x", color="#333", s=38, zorder=5)
            ax.annotate(f"{tr['realizedPnl']:+.0f}", (t1, tr["exit_price"]),
                        fontsize=7, color="#c0392b" if tr["realizedPnl"] < 0 else "#1e7b34",
                        xytext=(3, 5), textcoords="offset points")
        ax.set_title(f"{iid}  (triangle=entry, x=exit, orange=long, blue=short)")
        ax.set_ylabel("Price")
        ax.grid(alpha=0.25)
        ax.legend(loc="upper left", fontsize=8)

    ax_eq = axes[-1]
    ax_eq.plot(ts, eq, lw=1.2, color="#1e7b34", label="Equity")
    ax_eq.axhline(res["metrics"]["initial"], ls="--", lw=0.7, color="#999")
    ax_eq.set_title(f"Equity Curve — {title}")
    ax_eq.set_ylabel("Equity (USDT)")
    ax_eq.grid(alpha=0.25)
    ax_eq.legend()

    plt.tight_layout()
    plt.savefig(out_png, dpi=130)
    logger.info("图表已保存: %s", out_png)


# ----------------------------------------------------------------------
def print_metrics(m: Dict[str, Any]) -> None:
    print("\n" + "=" * 56)
    print("Seagull 策略回测结果")
    print("=" * 56)
    rows = [
        ("初始资金", f"${m['initial']:,.2f}"),
        ("期末权益", f"${m['final']:,.2f}"),
        ("总收益率", f"{m['total_return']:+.2%}"),
        ("年化收益率", f"{m['annual_return']:+.2%}"),
        ("最大回撤", f"{m['max_drawdown']:.2%}"),
        ("夏普比率", f"{m['sharpe']:.2f}"),
        ("交易次数", f"{m['n_trades']}"),
        ("胜率", f"{m['win_rate']:.1%}"),
        ("平均盈利", f"${m['avg_win']:.2f}"),
        ("平均亏损", f"-${m['avg_loss']:.2f}"),
        ("盈亏比", f"{m['profit_factor']:.2f}" if m['profit_factor'] != float('inf') else "∞"),
        ("最大连亏", f"{m['max_losing_streak']} 次"),
    ]
    for k, v in rows:
        print(f"  {k:<12}{v:>16}")
    print("=" * 56 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Seagull 策略回测")
    ap.add_argument("--config", default="config.json", help="配置文件")
    ap.add_argument("--insts", default=None, help="逗号分隔，如 BTC-USDT-SWAP,ETH-USDT-SWAP")
    ap.add_argument("--bar", default=None, help="K线周期，默认取配置")
    ap.add_argument("--bars", type=int, default=4320, help="模拟行情K线数量（默认4320≈45天@15m）")
    ap.add_argument("--csv", default=None, help="真实K线CSV（ts,open,high,low,close,vol）")
    ap.add_argument("--equity", type=float, default=10_000.0, help="初始资金")
    ap.add_argument("--seed", type=int, default=42, help="模拟行情随机种子")
    ap.add_argument("--out", default="data/backtest_result.png", help="输出图路径")
    args = ap.parse_args()

    cfg: Dict[str, Any] = {}
    if os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    if args.bar:
        cfg["bar"] = args.bar
    if args.insts:
        cfg["inst_ids"] = [i.strip() for i in args.insts.split(",") if i.strip()]
    bar = cfg.get("bar", "15m")
    inst_ids = cfg.get("inst_ids", ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    if args.csv:
        df = load_csv(args.csv)
        inst_ids = inst_ids[:1]
        df_map = {inst_ids[0]: df}
        logger.info("CSV 数据: %d 根 %s K线", len(df), bar)
    else:
        start_prices = {"BTC-USDT-SWAP": 65000.0, "ETH-USDT-SWAP": 3200.0}
        feed = SimulatedFeed(inst_ids, bar, start_prices, history_bars=1, seed=args.seed)
        df_map = {}
        for iid in inst_ids:
            rows = feed.generate(iid, args.bars)
            df_map[iid] = pd.DataFrame(rows)
        logger.info("模拟行情: %s × %s × %d 根 (seed=%d)", inst_ids, bar, args.bars, args.seed)

    res = run_backtest(cfg, df_map, initial_equity=args.equity)
    print_metrics(res["metrics"])

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    trades_csv = args.out.replace(".png", "_trades.csv")
    pd.DataFrame(res["trades"]).to_csv(trades_csv, index=False, encoding="utf-8-sig")
    logger.info("交易明细已保存: %s", trades_csv)
    plot_result(res, df_map, args.out, f"Seagull @ {bar} | {len(inst_ids)} insts")
    print(f"图表: {args.out}\n明细: {trades_csv}")


if __name__ == "__main__":
    main()
