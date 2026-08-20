"""
Web 仪表盘（FastAPI）。

独立线程运行（与交易循环隔离，单点崩溃不影响交易）。
默认监听 0.0.0.0:$PORT（Railway/Heroku 风格）— 这是 Railway 健康检查必须的 HTTP 端口。

端点：
  GET /              简洁 HTML 仪表盘（可手动刷新）
  GET /health        健康检查（Railway 用此确认存活）
  GET /status        JSON：引擎/熔断/持仓/权益
  GET /trades        JSON：最近 N 笔平仓交易
  GET /stats         JSON：累计绩效（平仓笔数/胜率/已实现/最大回撤）
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse

if TYPE_CHECKING:
    from .engine import SeagullEngine

logger = logging.getLogger("web")

# 仪表盘 HTML（轻量，单文件即可阅读，不需要前端构建）
DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Seagull 量化机器人</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #0f1115; color: #e6e6e6;
         margin: 0; padding: 24px; max-width: 920px; margin: 0 auto; }
  h1 { margin: 0 0 8px 0; font-size: 22px; }
  .sub { color: #888; font-size: 12px; margin-bottom: 24px; }
  .grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); margin-bottom: 20px; }
  .card { background: #1a1d24; border: 1px solid #2a2f3a; border-radius: 8px; padding: 14px; }
  .k { color: #8b95a5; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
  .v { font-size: 22px; font-weight: 600; margin-top: 6px; }
  .v.pos { color: #4ade80; }
  .v.neg { color: #f87171; }
  table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 12px; }
  th { text-align: left; color: #8b95a5; font-weight: 400; padding: 6px 8px; border-bottom: 1px solid #2a2f3a; }
  td { padding: 8px; border-bottom: 1px solid #2a2f3a; }
  .tag { padding: 2px 8px; border-radius: 4px; font-size: 10px; }
  .tag.live { background: #064e3b; color: #6ee7b7; }
  .tag.locked { background: #7c2d12; color: #fdba74; }
  .tag.noapi { background: #4b5563; color: #d1d5db; }
  .footer { margin-top: 30px; color: #555; font-size: 11px; }
  .ref { margin-top: 6px; font-size: 11px; }
  .ref a { color: #6b7280; text-decoration: none; }
  .ref a:hover { color: #9ca3af; }
</style>
</head><body>
<h1>🦅 Seagull 量化机器人</h1>
<div class="sub" id="meta">加载中……</div>
<div class="grid" id="cards"></div>
<h3 style="font-size:13px;color:#9ca3af;font-weight:400;margin:20px 0 6px 0;">当前持仓</h3>
<table id="positions"><thead><tr><th>合约</th><th>方向</th><th>张数</th><th>入场价</th><th>当前价</th><th>浮动盈亏</th><th>止损</th><th>止盈</th></tr></thead><tbody></tbody></table>
<h3 style="font-size:13px;color:#9ca3af;font-weight:400;margin:20px 0 6px 0;">最近平仓</h3>
<table id="trades"><thead><tr><th>时间</th><th>合约</th><th>方向</th><th>入场</th><th>出场</th><th>张数</th><th>已实现</th><th>原因</th></tr></thead><tbody></tbody></table>
<div class="footer">每 5 秒自动刷新 · Railway 健康检查：<code>/health</code></div>
<script>
async function load() {
  try {
    const r = await fetch('/status');
    if (!r.ok) throw new Error('HTTP '+r.status);
    const s = await r.json();
    document.getElementById('meta').textContent =
      `${s.exchange} | ${s.mode} | 周期 ${s.bar} | 轮询 ${s.poll_interval}s | 启动 ${s.uptime_seconds}s 前`;
    const lockedTag = s.day_locked ? '<span class="tag locked">熔断锁定</span>' : '<span class="tag live">可交易</span>';
    const eqClass = s.equity >= s.day_start_equity ? 'pos' : 'neg';
    document.getElementById('cards').innerHTML = `
      <div class="card"><div class="k">权益</div><div class="v ${eqClass}">$${s.equity.toFixed(2)}</div></div>
      <div class="card"><div class="k">当日基准</div><div class="v">$${s.day_start_equity.toFixed(2)} ${lockedTag}</div></div>
      <div class="card"><div class="k">当日回撤</div><div class="v neg">-$${s.day_drawdown.toFixed(2)}</div></div>
      <div class="card"><div class="k">累计已实现</div><div class="v ${s.total_realized>=0?'pos':'neg'}">$${s.total_realized.toFixed(2)}</div></div>
      <div class="card"><div class="k">平仓笔数</div><div class="v">${s.trade_count}</div></div>
      <div class="card"><div class="k">胜率</div><div class="v">${(s.win_rate*100).toFixed(1)}%</div></div>
      <div class="card"><div class="k">活跃持仓</div><div class="v">${s.open_positions.length}</div></div>
      <div class="card"><div class="k">连亏计数</div><div class="v">${s.losing_streak}</div></div>
    `;
    const posBody = document.querySelector('#positions tbody');
    posBody.innerHTML = (s.open_positions.length === 0)
      ? '<tr><td colspan="8" style="color:#6b7280;text-align:center;">无持仓</td></tr>'
      : s.open_positions.map(p => `
        <tr><td>${p.instId}</td>
          <td>${p.direction==='long'?'<span style="color:#f87171">多</span>':'<span style="color:#60a5fa">空</span>'}</td>
          <td>${p.contracts}</td><td>${p.entry_price.toFixed(2)}</td><td>${p.mark_price.toFixed(2)}</td>
          <td style="color:${p.upl>=0?'#4ade80':'#f87171'}">${p.upl>=0?'+':''}${p.upl.toFixed(2)}</td>
          <td>${p.sl!=null?p.sl.toFixed(2):'-'}</td><td>${p.tp!=null?p.tp.toFixed(2):'-'}</td>
        </tr>`).join('');
    const tBody = document.querySelector('#trades tbody');
    tBody.innerHTML = (s.recent_trades.length === 0)
      ? '<tr><td colspan="8" style="color:#6b7280;text-align:center;">尚无交易</td></tr>'
      : s.recent_trades.slice(0,10).map(t => `
        <tr><td>${new Date(t.uTime).toLocaleString()}</td>
          <td>${t.instId}</td>
          <td>${t.direction==='long'?'<span style="color:#f87171">多</span>':'<span style="color:#60a5fa">空</span>'}</td>
          <td>${t.entry_price.toFixed(2)}</td><td>${t.exit_price.toFixed(2)}</td>
          <td>${t.contracts}</td>
          <td style="color:${t.realizedPnl>=0?'#4ade80':'#f87171'}">${t.realizedPnl>=0?'+':''}${t.realizedPnl.toFixed(2)}</td>
          <td>${t.reason}</td></tr>`).join('');
  } catch (e) {
    document.getElementById('meta').textContent = '加载失败: '+e.message;
  }
}
load();
setInterval(load, 5000);
</script>
</body></html>"""


def _collect_status(engine: "SeagullEngine") -> Dict[str, Any]:
    """线程安全快照：把引擎当前状态打成 dict。"""
    try:
        equity = engine.client.get_equity()
    except Exception:
        equity = 0.0

    # 持仓（含 mark/upl/sl/tp）
    open_positions: List[Dict[str, Any]] = []
    for iid, trader in engine.traders.items():
        if trader.pos.is_open:
            try:
                tick = engine.client.get_ticker(iid)
                mark = float(tick.get("last", tick.get("markPx", 0)) or 0)
            except Exception:
                mark = trader.pos.entry_price
            # 合约浮动盈亏 = 方向 × 张数 × ctVal × (mark - entry)
            # 多：(mark - entry)，空：(entry - mark)
            ct_val = float(trader.inst.get("ctVal", 1))
            sign = 1 if trader.pos.direction == "long" else -1
            upl = sign * trader.pos.contracts * ct_val * (mark - trader.pos.entry_price)
            sl = trader.pos.sl_price or 0.0
            open_positions.append({
                "instId": iid,
                "direction": trader.pos.direction,
                "contracts": trader.pos.contracts,
                "entry_price": trader.pos.entry_price,
                "mark_price": mark,
                "upl": upl,
                "sl": sl,
                "tp": trader.pos.tp_price,
            })

    # 近期交易（最多 50 笔）
    trades: List[Dict[str, Any]] = []
    try:
        history = engine.client.get_positions_history(iid if engine.traders else "",
                                                    limit=50) if False else []
    except Exception:
        history = []
    # 用 trade_history 属性（PaperSwapClient / 部分 OKX 客户端）
    try:
        history = list(getattr(engine.client, "trade_history", []))[-50:]
    except Exception:
        history = []
    for t in history:
        trades.append({
            "instId": t.get("instId", ""),
            "direction": t.get("direction", "long"),
            "contracts": t.get("contracts", 0),
            "entry_price": t.get("entry_price", 0),
            "exit_price": t.get("exit_price", 0),
            "realizedPnl": t.get("realizedPnl", 0),
            "reason": t.get("reason", ""),
            "uTime": t.get("uTime", 0),
        })

    # 累计已实现
    total_realized = sum(t["realizedPnl"] for t in trades)
    wins = sum(1 for t in trades if t["realizedPnl"] > 0)
    win_rate = (wins / len(trades)) if trades else 0.0

    day_start = getattr(engine, "_day_start_equity", equity)
    day_drawdown = max(0.0, day_start - equity)
    day_locked = getattr(engine, "_day_locked", False)

    # 连亏计数（取所有 trader 的 strategy 中的最大值）
    losing_streak = max(
        (t.strategy.losing_streak for t in engine.traders.values()),
        default=0,
    )

    return {
        "exchange": engine.cfg.get("exchange", "okx").upper(),
        "mode": "纸面模式" if engine.paper else ("币安测试网" if engine.cfg.get("testnet") else "币安实盘"),
        "bar": engine.bar,
        "poll_interval": engine.poll_interval,
        "uptime_seconds": int(getattr(engine, "_uptime_s", lambda: 0)()),
        "equity": equity,
        "day_start_equity": day_start,
        "day_drawdown": day_drawdown,
        "day_locked": day_locked,
        "total_realized": total_realized,
        "trade_count": len(trades),
        "win_rate": win_rate,
        "losing_streak": losing_streak,
        "open_positions": open_positions,
        "recent_trades": trades,
    }


def create_app(engine: "SeagullEngine") -> FastAPI:
    app = FastAPI(title="Seagull Quant Bot", version="1.0")

    @app.get("/health")
    def health() -> Dict[str, Any]:
        # Railway 健康检查：必须返回 200
        return {"status": "ok", "engine_alive": engine.is_running()}

    @app.get("/status")
    def status() -> Dict[str, Any]:
        return _collect_status(engine)

    @app.get("/trades")
    def trades() -> Dict[str, Any]:
        st = _collect_status(engine)
        return {"count": len(st["recent_trades"]), "trades": st["recent_trades"]}

    @app.get("/stats")
    def stats() -> Dict[str, Any]:
        st = _collect_status(engine)
        return {
            "trade_count": st["trade_count"],
            "win_rate": st["win_rate"],
            "total_realized": st["total_realized"],
            "equity": st["equity"],
            "day_drawdown": st["day_drawdown"],
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(content=DASHBOARD_HTML)

    return app


def start_web_server(
    engine: "SeagullEngine",
    host: str = "0.0.0.0",
    port: Optional[int] = None,
) -> None:
    """
    在后台线程启动 FastAPI/uvicorn。**不阻塞**主交易循环。

    port: 默认读取环境变量 PORT（Railway/Heroku 自动注入），否则 8080。
    """
    import os
    import uvicorn

    if port is None:
        port = int(os.environ.get("PORT", 8080))

    app = create_app(engine)
    config = uvicorn.Config(
        app=app, host=host, port=port,
        log_level="warning",          # 减少访问日志刷屏
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, name="web-server", daemon=True)
    t.start()
    logger.info("[Web] 仪表盘已启动 http://%s:%d/  (Railway 健康检查 /health)", host, port)