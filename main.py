"""
Seagull × 多平台永续合约机器人入口。

用法：
  python main.py --config config.json           # 按 config.exchange 路由（okx/binance）
  python main.py --config config.json --paper   # 纸面模式：真实公开行情 + 本地撮合
  python main.py --demo 60                      # 演示：模拟行情跑 60 个周期（无需API）

首次使用请务必：
  1. config.json 填入 API Key
     - 币安：开通测试网 https://testnet.binancefuture.com，保持 testnet=true
     - OKX：开通模拟盘 Demo Trading，保持 simulated=true
  2. 用 backtest.py 验证参数后，再考虑实盘（风险自担）

⚠️ 切勿在公开聊天/代码仓库中泄露 API Key！若已泄露立即重置。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time


def setup_logging(log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(log_dir, "seagull.log"), encoding="utf-8"
            ),
        ],
    )


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sample = {
            "exchange": "binance",
            "api_key": "", "secret_key": "", "passphrase": "",
            "testnet": True, "simulated": True,
            "inst_ids": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            "bar": "15m",
            "poll_interval": 30,
            "paper_equity": 10000,
            "strategy": {
                "fast_ema": 50, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
                "atr_period": 14, "min_body_ratio": 0.45,
                "sl_atr": 1.5, "tp_atr": 2.0,
                "use_trailing": True, "trailing_start_atr": 1.0, "trailing_dist_atr": 0.8,
                "notional_per_order": 100.0, "reduced_notional": 0,
                "max_open_instruments": 3, "max_daily_loss_usd": 45.0,
                "losing_streak_to_reduce": 2,
            },
            "risk": {"leverage": 3, "td_mode": "isolated"},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2, ensure_ascii=False)
        print(f"未找到配置，已生成示例配置 {path}，请填入 API Key 后重试")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser(description="Seagull × 多平台永续合约机器人（OKX/币安）")
    ap.add_argument("--config", default="config.json", help="配置文件路径")
    ap.add_argument("--paper", action="store_true",
                    help="纸面模式：拉真实公开行情但本地撮合，不真实下单")
    ap.add_argument("--demo", type=int, default=0, metavar="N",
                    help="演示模式：模拟行情连续跑 N 个周期（无需 API Key）")
    args = ap.parse_args()

    setup_logging()
    logger = logging.getLogger("main")

    from okxquant.engine import SeagullEngine

    cfg = load_config(args.config)
    demo = args.demo > 0
    paper = args.paper or demo
    if demo:
        cfg["poll_interval"] = 0  # 演示模式连续推进

    engine = SeagullEngine(cfg, paper=paper, state_file="state.json")

    if demo:
        logger.info("演示模式：模拟行情推进 %d 个周期……", args.demo)
        for i in range(args.demo):
            engine._tick()
            time.sleep(0.02)
        eq = engine.client.get_equity()
        trades = engine.client.trade_history  # type: ignore[attr-defined]
        logger.info("=" * 56)
        logger.info("演示结束：equity=%.2f，共 %d 笔平仓交易", eq, len(trades))
        for t in trades:
            logger.info(
                "  %s %s %d张 %.2f→%.2f [%s] PnL=%+.2f",
                t["instId"], t["direction"], t["contracts"],
                t["entry_price"], t["exit_price"], t["reason"], t["realizedPnl"],
            )
        logger.info("=" * 56)
    else:
        engine.run()


if __name__ == "__main__":
    main()
