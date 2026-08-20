# Seagull × 多平台永续合约量化机器人

把你 MT5 上的 **Seagull_Adaptive_Pro** EA 完整转化为多平台永续合约版本，**支持 OKX 和币安 (Binance USDⓈ-M Futures)**，交易 BTC、ETH 等加密货币。**回测与实盘共用同一套代码路径**——回测验证的就是实盘执行的。

通过 `config.exchange` 字段一键切换平台（`okx` / `binance`），策略层、风控、执行器完全无感。

---

## 1. 策略逻辑（与 EA 一一对应）

| MT5 EA 原逻辑 | 本实现 | 配置文件 |
|---|---|---|
| `close[1] > EMA50` 趋势过滤 | `df.close.iloc[-1] > ema(close, fast_ema)` | `strategy.fast_ema=50` |
| `InpMinBodyRatio=0.45` 实体动能 | `\|c-o\|/(h-l) >= min_body_ratio` | `strategy.min_body_ratio=0.45` |
| MACD(12,26,9) 主线 > 信号线 | `dif > dea` | `strategy.macd_fast/slow/signal` |
| 多单 `SL=ask-1.5·ATR` `TP=ask+2·ATR` | 开仓后挂 OCO 止盈止损（交易所侧触发） | `strategy.sl_atr/tp_atr` |
| 空单（对称补全） | `close<EMA50` + 阴线 + 实体 + `dif<dea` | 同上 |
| `InpUseTrailing` 微型追踪锁利 | 浮盈 ≥ 1·ATR 启动，止损跟随保持 0.8·ATR 距离，**至少保本**，只朝有利方向移动 | `strategy.use_trailing/trailing_start_atr/trailing_dist_atr` |
| 连续亏损 ≥ 2 → 手数降至 0.01 | 名义价值降至 `reduced_notional`（0=最小张数） | `strategy.losing_streak_to_reduce/reduced_notional` |
| `InpMaxDailyLossUSD=45` 日内熔断 | 引擎按 equity 监控日内回撤，触发后当日停止开仓（**持仓止损仍生效**） | `strategy.max_daily_loss_usd` |
| `PositionsTotal() >= 3` 不开新仓 | 同时最多持有 `max_open_instruments` 个币种仓位 | `strategy.max_open_instruments` |

> **重要差异**：原 EA 交易 XAUUSD 0.03 手 ≈ $7200 名义（按金价 2400 计）。本项目按你配置的 `notional_per_order` USDT 名义计算合约张数，**默认 100 USDT/单**——更保守，开 1 张 BTC 永续（≈$650 保证金 @10x）即可作为起步；想要接近 EA 原风险敞口，把 `notional_per_order` 调到 7000+ 即可。

---

## 2. 项目结构

```
okx-quant-bot/
├── README.md                # 本文档
├── requirements.txt         # Python 依赖
├── config.example.json      # 配置示例（复制为 config.json 再填密钥）
├── config.binance.example.json  # 币安专用配置示例
├── main.py                  # 入口：实盘/模拟盘/纸面/演示
├── backtest.py              # 回测入口（同一份策略代码）
├── data/                    # 回测产物（图表 + 交易明细）
│   ├── backtest_btc.png
│   └── backtest_btc_trades.csv
└── okxquant/
    ├── base_client.py       # ★ 统一 SwapClientBase 协议 + Mixin
    ├── client.py            # OKX V5 REST 客户端（签名/重试/时间校准）
    ├── swap_client.py       # OKX 永续合约扩展（下单/OCO/改algo/持仓/权益/历史）
    ├── binance_swap_client.py  # ★ 币安 USDⓈ-M 永续客户端
    ├── exchange_error.py    # 统一异常基类（ExchangeApiError）
    ├── ws.py                # WebSocket 公共行情订阅（备选）
    ├── indicators.py        # SMA/EMA/RSI/ATR/布林/MACD
    ├── strategy.py          # 现货策略集（双均线/网格/RSI，附带）
    ├── seagull.py           # ★ Seagull 策略（实盘回测共用）
    ├── position.py          # 现货持仓跟踪
    ├── risk.py              # 现货风控
    ├── trader.py            # 现货执行
    ├── swap_trader.py       # ★ 合约执行（张数换算/OCO/追踪止损/连亏统计，与平台无关）
    ├── paper.py             # ★ 纸面撮合（回测+无密钥纸面共用）
    └── engine.py            # ★ 多币种轮询引擎，按 config.exchange 路由不同客户端
```

---

## 3. 快速开始

### 3.1 安装依赖

```bash
pip install -r requirements.txt
```

### 3.2 三种"零成本"运行模式（**无需 API Key**）

| 模式 | 命令 | 数据源 | 撮合 | 用途 |
|---|---|---|---|---|
| **回测** | `python backtest.py` | 模拟行情 | 本地纸面 | 验证策略与参数 |
| **演示** | `python main.py --demo 5000` | 模拟行情 | 本地纸面 | 跑通"信号→开仓→OCO→追踪→平仓"全链路 |
| **纸面（实行情）** | `python main.py --paper` | OKX 真实公开行情 | 本地纸面 | 检验实行情数据接入，不真实下单 |

#### 演示运行

```bash
# 5000 个 15m 周期 ≈ 52 天模拟数据，2~3 分钟跑完
python main.py --demo 5000
```

输出示例（尾部）：
```
演示结束：equity=10906.55，共 606 笔平仓交易
  BTC-USDT-SWAP short 1张 255863.43→249559.00 [止盈] PnL=+60.52
  ETH-USDT-SWAP long 1张 1738.21→1776.07 [止盈] PnL=+3.61
```

#### 回测 + 出图

```bash
# 默认 BTC+ETH 15m，4320 根 ≈ 45 天
python backtest.py

# 自定义：只跑 BTC，5000 根
python backtest.py --insts BTC-USDT-SWAP --bars 5000

# 用你自己的 K 线 CSV
python backtest.py --csv data/btc_15m.csv --insts BTC-USDT-SWAP
```

CSV 列：`ts(秒或毫秒),open,high,low,close,vol`

回测输出：
```
========================================================
Seagull 策略回测结果
========================================================
  初始资金            $10,000.00
  期末权益            $10,236.21
  总收益率                +2.36%
  年化收益率              +31.34%
  最大回撤                -0.87%
  夏普比率                  5.66
  交易次数                   416
  胜率                     57.0%
  平均盈利                 $6.27
  平均亏损                -$6.99
  盈亏比                   1.19
  最大连亏                   7 次
========================================================
```

图表 `data/backtest_btc_eth.png` 含：每币种价格 + 买卖点标注 + 净值曲线。

### 3.3 模拟盘/实盘（需要 API Key）

通过 `config.exchange` 字段选择平台，**`okx` 或 `binance`**，二选一。

#### 方案 A：币安 USDⓈ-M 永续（推荐先用测试网）

**第一步：申请测试网 API Key**

1. 打开 [Binance Futures Testnet](https://testnet.binancefuture.com) → 用 GitHub 登录
2. 右上角 **API Key** → 系统会自动生成一个 key/secret（**已含测试网 USDT，无需充值**）
3. 妥善保存 key/secret（关闭后不再显示）

**第二步：填入 config.json**

```json
{
  "exchange": "binance",                    // ★ 切到币安
  "api_key": "你的测试网API_KEY",
  "secret_key": "你的测试网SECRET",
  "testnet": true,                          // ★ 测试网开关（true 才行）
  "inst_ids": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
  "bar": "15m",
  "poll_interval": 30,
  "strategy": {
    "fast_ema": 50, "min_body_ratio": 0.45,
    "sl_atr": 1.5, "tp_atr": 2.0,
    "use_trailing": true, "trailing_start_atr": 1.0, "trailing_dist_atr": 0.8,
    "notional_per_order": 100,
    "max_open_instruments": 3,
    "max_daily_loss_usd": 45,
    "losing_streak_to_reduce": 2
  },
  "risk": {"leverage": 3, "td_mode": "isolated"}
}
```

**第三步：跑测试网**

```bash
python main.py
```

日志会显示 `[币安测试网(Binance Testnet)]`。先观察信号生成、OCO 挂单、追踪止损是否正常，确认无误后再考虑实盘。

> 实盘切换：把 `testnet` 改为 `false`，并把 key/secret 换成**实盘**的（[创建](https://www.binance.com/en/my/settings/api-management) 时只勾选 **Enable Futures**，禁用提币、绑定服务器 IP）。

#### 方案 B：OKX V5 永续

**第一步：申请 OKX 模拟盘 API Key**

1. 登录 OKX → 顶部导航 **交易** → 右上角齿轮 **模拟盘**（Demo Trading）
2. **API 管理** → 创建 API Key（仅勾选**交易**权限，资金划转/提币**不要勾**）
3. 妥善保存：**API Key / Secret Key / Passphrase**（Secret 关闭后不再显示）

**第二步：填入 config.json**

```json
{
  "exchange": "okx",                        // ★ 切到 OKX
  "api_key": "你的-KEY",
  "secret_key": "你的-SECRET",
  "passphrase": "你的-PASSPHRASE",
  "simulated": true,                        // ★ 模拟盘
  "inst_ids": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
  ...
}
```

**第三步：跑模拟盘**

```bash
python main.py
```

> 模拟盘跑稳定后，**把 `simulated` 改为 `false` 即为实盘**。强烈建议先把 `notional_per_order` 调到 50-100 USDT/单作为测试。

---

#### 平台间关键差异（已自动处理）

| 维度 | OKX | 币安 |
|---|---|---|
| API 签名 | HMAC-SHA256（base64 编码） | HMAC-SHA256（hex 编码） |
| OCO 实现 | 原生 OCO 条件单 | `TAKE_PROFIT_MARKET` + `STOP_MARKET` 两腿 reduce-only |
| 修改止损 | `amend-algos` 改触发价 | 撤旧腿 + 挂新腿（trader 透明） |
| 数量单位 | 整数张（ctVal=0.01 BTC） | 浮点币（quantity=0.001 BTC） |
| 单向持仓 | `posMode=net_mode` | `dualSidePosition=false` |
| 标记价触发 | 触发价字段可选 | 默认 `MARK_PRICE` 防止插针 |

> 统一接口 `SwapClientBase`（`okxquant/base_client.py`）保证 `SwapTrader` 和 `SeagullEngine` **完全无感**——切换平台只改 `config.exchange` 一个字段。

---

## 4. 关键设计要点

### 4.1 交易所侧止盈止损（**保命优先**）
- 开仓后立刻挂 OCO 条件单（reduceOnly，tp/sl 触发后市价成交）
- 机器人掉线/崩溃也**不会裸奔**——交易所侧永远有硬止损
- 追踪止损用 `POST /api/v5/trade/amend-algos` 移动 SL 触发价；失败时原止损保留

### 4.2 策略/执行/撮合完全解耦
```
SeagullStrategy  ──→  SeagullSignal  ──→  SwapTrader  ──→  OkxSwapClient / PaperSwapClient
   (信号生成)         (意图表达)         (张数换算/OCO)        (接口实现，真实或纸面)
```
- 同一份 `SeagullStrategy + SwapTrader` 在回测、纸面、实盘三种模式中**完全相同**
- 回测用 `PaperSwapClient` 跑出的结果 = 同一份代码在实盘能跑出来的行为

### 4.3 状态持久化
`state.json` 记录每个币种的持仓镜像、追踪 SL/OCO algoId、连续亏损计数、日内熔断状态。**进程崩溃/重启后自动恢复**；如本地无记录但交易所有仓，会从 `GET /fapi/v2/positionRisk`（币安）或 `/api/v5/account/positions`（OKX）+ pending algos 重建镜像。

### 4.4 纸面撮合保守假设
- 滑点：1 tick（向不利方向）
- 手续费：taker 0.05%（双向）
- K 线内止损/止盈**同时触及**时按"先触发止损"处理（悲观）

---

## 5. ⚠️ 安全提示

**API Key 切勿在公开聊天、Issue、截图、代码仓库中泄露！**

如果你的 key **已经** 出现在任何公开场合（例如本对话历史），请立即：
1. 登录交易所后台 → API 管理 → **删除该 key**
2. 创建新 key，**绑定服务器 IP**、**只勾选必要的交易权限**、**禁用提币**
3. 新 key 妥善保存在 `config.json`（已被 `.gitignore` 忽略，不会被 git 追踪）

推荐安全配置：
- 币安：勾选 `Enable Futures`，**关闭** Enable Spot、Enable Withdrawals、Enable Margin
- OKX：仅勾选 `交易`，**关闭** 提币 / 资金划转

---

## 6. 风险提示

加密货币永续合约带杠杆，**可能损失全部保证金**。本项目仅作技术研究，使用前请：
- 在测试网/模拟盘充分验证参数与稳定性
- 理解策略逻辑（EMA/MACD/ATR 趋势跟踪在小级别K线容易假信号）
- 设置合理的 `notional_per_order`、`leverage`、`max_daily_loss_usd`
- 评估所在司法管辖区对加密货币交易的合规性
- 切勿投入无法承受损失的资金

---

## 5. 风险与合规

> ⚠️ **加密货币永续合约带杠杆，**可能损失全部保证金。本项目仅作技术研究与学习。

- **加密货币衍生品交易在许多司法管辖区受到严格监管**（包括中国境内 2021 年的九部委通知）。请确保你所在地区合法合规后再使用。
- **永远先在测试网/模拟盘验证**（币安 `testnet=true` / OKX `simulated=true`）→ 跑稳定后再切实盘，且务必以你能承受损失的金额起步。
- API Key 申请时**只勾"交易"权限**，**绝对不要勾"提币/划转"**，**绑定服务器 IP**。
- `config.json` **不要提交到 Git**（已 `.gitignore`），Secret Key 一旦泄露立刻到对应平台撤销并重置。
- 机器人自带的风控（日内熔断、连亏降仓、SL/TP 交易所侧触发、追踪锁利）**不能替代你自己对风险的把控**。本项目作者不对任何交易损失负责。

---

## 6. 常见问题

**Q1: EA 原代码里空单逻辑和追踪止损被截断了，我按对称补全的对吗？**
A: 是的。多单 EA 已写明，**空单按对称逻辑补全**（close<EMA50 + 阴线 + 实体≥45% + `dif<dea`，SL=bid+1.5ATR / TP=bid-2ATR）。追踪止损按"标准微型追踪锁利"实现（浮盈≥1ATR 启动，止损保持 0.8ATR 距离且不低于保本价），全部参数可调。

**Q2: `notional_per_order` 该设多少？**
A: 经验值：
- 保守起步：50–100 USDT/单
- 接近 EA 原 XAUUSD 0.03 手敞口（≈ $7200 名义）：设为 7000+
- 关键不是金额本身，而是**单笔占你账户净值的比例**——单笔风险（SL 距离 × 名义）建议 < 账户净值的 1-2%

**Q3: 为什么默认 `td_mode=isolated`？**
A: 逐仓模式下每个币种亏损只损失该仓位保证金，**不会污染其他币种**或整个账户。盈透风控更友好。杠杆默认 3x 偏保守，加密永续波动大不建议超过 10x。

**Q4: 如何只交易 BTC 不开 ETH？**
A: 改 `config.json` 的 `inst_ids` 为 `["BTC-USDT-SWAP"]`。

**Q5: 日内熔断触发后怎么办？**
A: 当日**停止开仓**（持仓的 SL/TP 仍然生效，仍可被交易所自动平仓）。次日凌晨首次轮询时会自动重置权益基准、解除熔断。

**Q6: 连亏降仓后如何恢复？**
A: 任意一笔盈利平仓后 `losing_streak` 清零，恢复到 `notional_per_order`。这也是 EA 的语义。

**Q7: 我想用真实 K 线数据回测怎么办？**
A: 把 K 线导出成 CSV（`ts,open,high,low,close,vol`），运行：
```bash
python backtest.py --csv mydata.csv --insts BTC-USDT-SWAP
```
时间戳支持秒或毫秒。如果数据来源是 Binance：`https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=1000` 即可拉取。

**Q8: 怎么从 OKX 切换到币安（或反过来）？**
A: 改 `config.json` 的 `exchange` 字段（`"okx"` 或 `"binance"`），对应修改 `api_key` / `secret_key` / `testnet` 或 `simulated`。策略层、风控、执行器全部不用动，引擎自动路由到对应客户端。

**Q9: 币安和 OKX 的 OCO 行为有差异吗？**
A: OKX 是**原生 OCO**（一个 algoId 含两腿，触发自动撤另一腿）。币安永续**无原生 OCO**，本项目用 `TAKE_PROFIT_MARKET` + `STOP_MARKET` 两个 reduce-only 条件单组合实现，algoId 格式为 `tp|sl`——对策略层透明，触发后由 `trader.maintain()` 自动清理另一腿。

**Q10: 币安合约的"张数"语义跟 OKX 不一样，怎么处理的？**
A: 币安 USDⓈ-M 永续 `quantity` 是浮点币数（如 0.001 BTC），OKX `sz` 是整数张数（每张 0.01 BTC）。客户端在 `get_instrument` 时把币安 `stepSize` 暴露为 `ctVal` 字段，策略层始终用**整数内部张数** `contracts` 管理仓位，下单时 `quantity = contracts * ctVal`，对策略层完全透明。

---

## 7. 下一步可扩展

- **更多币种**：直接在 `inst_ids` 数组添加 `"SOL-USDT-SWAP"` 等
- **WebSocket 实时行情**：见 `okxquant/ws.py`，已实现但引擎默认 REST 轮询（更稳定）
- **资金费率**：永续合约 8h 收一次费率（通常 ±0.01%），可加入 `seagull.py` 作为交易成本/反向平仓信号
- **钉钉/飞书/Telegram 通知**：在 `SwapTrader._on_position_closed` / 引擎熔断处加 webhook 即可
- **多策略并行**：照 `seagull.py` 写新策略类，引擎中 `self.traders[iid] = SwapTrader(client, strategy_xxx, ...)`

---

## 8. 致谢

本项目是基于 [OKX V5 API](https://www.okx.com/docs-v5/zh-cn/) 和 [Binance USDⓈ-M Futures API](https://binance-docs.github.io/apidocs/futures/en/) 实现的 Seagull EA 转化，策略源自你提供的 MT5 EA 源码。
