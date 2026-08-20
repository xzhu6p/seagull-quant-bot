"""
Seagull × 多平台永续合约机器人入口。

用法：
  python main.py --config config.json           # 实盘/模拟盘 + Web 仪表盘（默认）
  python main.py --config config.json --paper   # 纸面模式 + Web 仪表盘
  python main.py --config config.json --no-web  # 关闭 Web 仪表盘（纯命令行运行）
  python main.py --demo 60                      # 演示：模拟行情跑 60 个周期（无需API）

部署到 Railway/Heroku 风格平台时：
  - 默认会自动监听 PORT 环境变量（通常是 8080）
  - 浏览器访问 http://<host>:<PORT>/  查看仪表盘
  - /health 端点是平台健康检查（必须返回 200）
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
    """从 config.json 加载，环境变量覆盖敏感字段（部署到 PaaS 时不必把密钥写进文件）。"""
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
            "web": {"enabled": True, "port": None},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2, ensure_ascii=False)
        print(f"未找到配置，已生成示例配置 {path}，请填入 API Key 后重试")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 环境变量覆盖（PaaS 部署的标准做法：密钥不进代码仓库）
    env_map = {
        "API_KEY":     ("api_key",      str),
        "SECRET_KEY":  ("secret_key",   str),
        "PASSPHRASE":  ("passphrase",   str),
        "EXCHANGE":    ("exchange",     str),
        "TESTNET":     ("testnet",      lambda x: x.lower() in ("1", "true", "yes", "on")),
        "SIMULATED":   ("simulated",    lambda x: x.lower() in ("1", "true", "yes", "on")),
        "BAR":         ("bar",          str),
        "POLL_INTERVAL": ("poll_interval", int),
        "INST_IDS":    ("inst_ids",     lambda x: [s.strip() for s in x.split(",") if s.strip()]),
    }
    for env_k, (cfg_k, cast) in env_map.items():
        v = os.environ.get(env_k)
        if v is not None and v != "":
            try:
                cfg[cfg_k] = cast(v)
            except Exception:  # noqa: BLE001
                pass

    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Seagull × 多平台永续合约机器人（OKX/币安）")
    ap.add_argument("--config", default="config.json", help="配置文件路径")
    ap.add_argument("--paper", action="store_true",
                    help="纸面模式：拉真实公开行情但本地撮合，不真实下单")
    ap.add_argument("--demo", type=int, default=0, metavar="N",
                    help="演示模式：模拟行情连续跑 N 个周期（无需 API Key）")
    ap.add_argument("--no-web", action="store_true",
                    help="关闭 Web 仪表盘（纯命令行运行）")
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
        # 演示模式不启 Web，纯跑完退出
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
        # 启动 Web 仪表盘（默认开启；--no-web 关闭）
        web_cfg = cfg.get("web", {})
        if web_cfg.get("enabled", True) and not args.no_web:
            try:
                from okxquant.web import start_web_server
                start_web_server(engine, port=web_cfg.get("port"))
            except Exception as e:  # noqa: BLE001
                logger.warning("Web 仪表盘启动失败（不影响交易）: %s", e)

        engine.run()


if __name__ == "__main__":
    main()