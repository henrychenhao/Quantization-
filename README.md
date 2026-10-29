---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'fc7d0fae-4297-4f9d-a454-8fea4e26e726'
  PropagateID: 'fc7d0fae-4297-4f9d-a454-8fea4e26e726'
  ReservedCode1: 'b114ea71-e90e-42a5-9ade-c4d88d0436be'
  ReservedCode2: 'b114ea71-e90e-42a5-9ade-c4d88d0436be'
---

# A股半自动量化系统

本地运行的 A 股量化终端：**信号 → 人工下单 → 记账回填**。系统只负责扫描行情、生成交易信号并推送提醒，**不自动下单**，所有交易由你在券商手动完成，再回填真实成交价与数量，账本自动更新。

数据全部保存在本机（SQLite + Parquet），无需上传云端。

## 功能

- **回测工作台**：多策略同区间对比，净值 / 回撤曲线、买卖点标记、样本内/样本外（walk-forward 70/30）验证
- **自选监控**：绑定策略、实时行情列（15 秒刷新）、一键运行信号扫描、自动补全标的名称
- **信号中心**：待处理信号卡片、历史流水、策略战绩（胜率与 1/5/20 日平均收益）
- **账户账本**：由成交流水推导持仓与现金、交易记录可纠错、资产走势图
- **AI 分析**：接入大模型（DeepSeek 兼容 OpenAI 接口），基于本地真实数据做个股诊断、持仓复盘、信号解读与策略问答
- **定时调度**：交易日心跳、尾盘扫描、收盘数据同步与备份、盘中分钟级轮询
- **企微推送**：信号、心跳消息可推送到企业微信群机器人（未配置时自动降级为本地日志）

## 快速开始

环境要求：Python 3.12、macOS / Linux。

```bash
# 1. 安装依赖
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 初始化配置（config.yaml 不入库，含个人凭据）
cp config.example.yaml config.yaml
# 按需填写：初始资金、自选标的、企微 webhook、大模型 api_key

# 3. 启动 Web 控制台
python main.py serve --port 8000
# 打开 http://127.0.0.1:8000
```

命令行回测：

```bash
python main.py backtest --code 513530 --strategy double_ma \
    --start 2023-10-21 --end 2026-10-21 --capital 100000
```

启动定时调度（常驻）：

```bash
python main.py schedule
```

## 内置策略

| 策略 | 代码 | 逻辑 |
| --- | --- | --- |
| 双均线交叉 | `double_ma` | MA5 上穿 MA20 买入，下穿卖出 |
| MACD 金叉死叉 | `macd` | DIF 上穿 DEA 买入，下穿卖出 |
| 布林带均值回归 | `boll_mr` | 跌破下轨买入，回到中轨上方卖出 |
| 唐奇安通道突破 | `donchian` | 突破 20 日最高买入，跌破 10 日最低卖出 |
| RSI 超卖回归 | `rsi` | RSI(14) 下穿 30 买入，上穿 70 卖出 |
| 网格交易（多档） | `grid` | 锚点下方每跌 3% 买一档（最多 5 档、每档 20% 资金），涨到均价 +3% 止盈清仓 |

## 信号五道闸门

1. **价格校验**：现价有效（非空非零），停牌直接跳过
2. **涨跌停过滤**：涨停买不进、跌停卖不出，信号无意义
3. **资金/持仓校验**：买入需现金覆盖（不足收缩股数，不足一手跳过）；卖出需有持仓
4. **去重校验**：同一标的 × 同一策略当日只推一次（含已忽略，忽略即当日安静）
5. **组合级风控**：买入后总仓位、单标的占比、最低现金预留上限（`config.yaml` 的 `risk` 段，全为 0 时关闭）

## 定时任务

| 时间 | 任务 |
| --- | --- |
| 交易日 09:35 | 心跳推送 + 资产快照 |
| 交易日 14:50 | 尾盘信号扫描 |
| 交易日 17:30 | 收盘 K 线刷新 → 信号质量结算 → 数据备份（保留最近 14 份） |
| 交易时段每 5 分钟 | 盘中实时轮询（新浪实时价，可配置关闭） |

`deploy/` 下提供 macOS launchd 配置（调度常驻 / 控制台常驻 / 防休眠保活），按需安装。

## 目录结构

```
├── main.py                  # 入口：backtest / serve / schedule
├── config.example.yaml      # 配置模板（复制为 config.yaml 使用）
├── app/
│   ├── config.py            # 配置加载
│   ├── db.py                # SQLite：自选/信号/流水/快照，含迁移
│   ├── indicators.py        # MA / MACD / BOLL / RSI / 唐奇安
│   ├── strategy/            # 六大策略实现 + 注册表
│   ├── backtest/            # 事件驱动回测引擎 + 绩效报告（含样本外验证）
│   ├── datasource/          # 行情源（akshare + 新浪实时）与 K 线本地缓存
│   ├── engine/              # 信号引擎 / 调度器 / 推荐 / 信号结算 / 备份
│   ├── account/             # 账本：流水推导持仓与现金、纠错
│   ├── notify/              # 企微推送
│   ├── llm/                 # 大模型客户端 + 本地数据上下文构建
│   └── web/                 # FastAPI 控制台（模板 + 静态资源）
├── tests/                   # 96 个自动化用例（隔离临时库，不触碰真实数据）
└── deploy/                  # launchd 配置
```

## 数据与隐私

- 账本数据库：`data/quant.db`（SQLite）
- 行情缓存：`data/kline/*.parquet`
- 每日备份：`data/backup/YYYY-MM-DD/`（数据库 + K 线，保留最近 14 份）
- `config.yaml`、`data/`、`logs/` 均已加入 `.gitignore`，**不会进入版本库**

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

测试使用独立的临时数据库（`QUANT_DB_PATH`），行情与推送全部打桩，不触网、不写真实账本。

## 免责声明

本项目仅用于学习与研究，所有信号、回测结果与 AI 分析均不构成投资建议。股市有风险，交易决策与盈亏由使用者自行承担。

> AI生成