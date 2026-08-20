# Railway / Heroku / 任意 PaaS 部署说明

## Railway 一键部署

1. 把项目推到 GitHub（已完成）
2. Railway → New Project → Deploy from GitHub → 选 `xzhu6p/seagull-quant-bot`
3. Railway 自动识别 Python + requirements.txt，装依赖
4. **变量 (Variables) 标签页**，必须设置以下环境变量（**不要写到代码里**）：

| 变量 | 值 | 说明 |
|---|---|---|
| `API_KEY` | 你的币安 API Key | testnet.binancefuture.com 创建 |
| `SECRET_KEY` | 你的币安 Secret | 配对上面那个 |
| `EXCHANGE` | `binance` | 固定 |
| `TESTNET` | `true` | 先用测试网 |
| `INST_IDS` | `BTC-USDT-SWAP,ETH-USDT-SWAP` | 英文逗号分隔 |
| `BAR` | `15m` | K线周期 |
| `POLL_INTERVAL` | `30` | 轮询秒数 |
| `PORT` | Railway 自动注入（通常 8080） | **不要手动设** |

5. Deploy → 等待启动 → Settings → 复制公开域名
6. 浏览器访问 `https://<你的域名>.up.railway.app/` → 看到仪表盘即成功

## ⚠️ Railway 免费计划限制

- **空闲 3-5 分钟后会休眠**，下次访问需要几秒"冷启动"
- **休眠期间交易循环暂停**——不推荐生产用，跑策略还是 VPS 更靠谱
- 免费额度 $5/月，够这个机器人消耗

## 健康检查

Railway 会定期访问 `/health`，必须返回 200。

## 本地启动

```bash
# 纸面（无需 API Key）
python main.py --paper --config config.json

# 币安测试网（需要 API Key 写在 config.json 或环境变量）
python main.py --config config.json

# 演示
python main.py --demo 2000
```

## 配置文件优先级

`main.py` 读取 `config.json`，但**环境变量会覆盖** config.json 里的相同字段：
- `API_KEY` → `cfg["api_key"]`
- `SECRET_KEY` → `cfg["secret_key"]`
- `EXCHANGE` → `cfg["exchange"]`
- `TESTNET` → `cfg["testnet"]`

这样 Railway 上**不必把密钥写到 config.json**（安全），部署时只在变量标签页填即可。