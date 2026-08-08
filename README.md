# AI 自动交易体验机

一个**三引擎对比**的仿真量化交易系统：`DeepSeek 大模型`（纯价格 / 价格+政策）与 `内置规则` 各用一个独立虚拟账户（默认 100 万虚拟资金），在同一份行情上并行交易，长期留痕，用于**客观评估 AI 决策的真实水平**。

> ⚠️ 本项目是学习/评测工具，**不构成投资建议，不保证收益**。当前仅仿真，不连接任何真实账户。

## 特性

- **三引擎对比**：规则（双均线）vs AI·纯价格 vs AI·价格+政策，独立账本、同源数据、可对比
- **零风险仿真**：虚拟资金，风控写死（单笔 ≤ 30% 总资产、单日买入 ≤ 50%）
- **全量留痕**：每个决策保存"输入行情 + AI 原始输出"，任何一笔交易可回溯
- **可测试**：核心逻辑为纯函数，28 个 pytest 用例覆盖
- **工程化**：pydantic 配置、`.env` 密钥、结构化日志、SQLite 持久化、matplotlib 报表
- **自动运行**：一行脚本注册 Windows 定时任务

## 目录结构

```
ai_trader/
├── aitrader/               # 核心包
│   ├── config.py           # pydantic 配置 + .env 密钥
│   ├── models.py           # 领域模型（账本/决策/成交）
│   ├── database.py         # SQLite 仓储
│   ├── datasource.py       # 行情数据源 + 政策数据源（财联社，可注入 mock）
│   ├── engines/            # 决策引擎（base / rule / deepseek）
│   ├── risk.py             # 风控（纯函数）
│   ├── portfolio.py        # 账本执行（纯函数）
│   ├── batch.py            # 每日流程编排
│   ├── backtest.py         # walk-forward 回测（指标 + 基准）
│   └── reporter.py         # 报表
├── tests/                  # pytest 测试
├── docs/                   # SRS.md 需求 / DESIGN.md 设计
├── scripts/                # 定时任务部署脚本
├── run.py                  # CLI 入口
└── config.json             # 业务配置
```

## 快速开始

### 1. 安装依赖

```
C:\veighna_studio\python.exe -m pip install -r requirements.txt
```

### 2. 配置 DeepSeek API Key（可选）

复制 `.env.example` 为 `.env`，填入 Key（申请地址：<https://platform.deepseek.com> → API Keys）。

> 不填 Key 也能跑，会退回到内置规则引擎。

### 3. 手动跑一次

```
cd ai_trader
C:\veighna_studio\python.exe run.py
```

### 4. 每天自动跑（可选）

在普通 PowerShell 中执行（无需管理员）：

```
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

它会配置两套机制，互相兜底：
- **每天 15:30 定时任务**（schtasks）
- **登录时启动项**：开机后自动补跑一次，解决“15:30 没开机”的遗漏

卸载：`scripts\uninstall_task.ps1`

## CLI 用法

```
python run.py                     # 三引擎跑今日（非交易日自动跳过；该日已跑过自动跳过）
python run.py --engine rule       # 仅规则引擎
python run.py --engine ai         # 仅 AI·纯价格（需 .env 配 Key）
python run.py --engine ai_policy  # 仅 AI·价格+政策
python run.py --date 2026-08-06   # 指定交易日（回放，只取截至该日行情，无前视偏差）
python run.py --force             # 该日已处理过也强制重跑
python run.py --report-only       # 只出报表
```

## 回测评估

用历史行情逐日回放引擎决策（独立数据库，不污染每日账本），评估策略真实水平：

```
python run.py --backtest --start 2026-05-01 --end 2026-08-01   # 回测（默认全部引擎）
python run.py --backtest --engine rule --start ...              # 只回测规则引擎
python run.py --backtest --benchmark 510300                     # 指定基准标的
```

输出：每个引擎的总收益 / 年化 / 最大回撤 / 夏普 / 胜率 / 盈亏比 / 换手率（双边口径），与基准（买入持有）对比；`reports/backtest_report.html` 可交互查看。AI 引擎的 API 响应会缓存到 `data/ai_response_cache.json`（上限 2000 条，防无限膨胀），重复回测不重复计费。

> 回测**默认跳过 `AI·政策版`**：回测无法获取历史政策，政策版会退化为纯价格版、产生重复曲线误导；如确需回测请用 `--engine ai_policy`（会给出提示）。

## 政策参考

`AI·价格+政策` 引擎每天收盘后拉取财联社宏观政策快讯（央行/证监会/降息/监管等关键词过滤），连同行情一起喂给 DeepSeek 综合决策，与纯价格版本对比，用于客观评估“政策信息对 AI 决策是否有帮助”。

关键词可在 `config.json` 的 `policy.keywords` 中调整。

## 测试

```
python -m pytest -q
```

## 怎么看结果

- **`reports/daily_report.html`：每日日报（推荐）** —— 双击用浏览器打开，绿=赚/红=亏，大字显示累计盈亏、持仓、资金曲线，小白友好
- **`reports/backtest_report.html`：回测报告** —— 回测区间各引擎指标（年化/回撤/夏普/胜率/盈亏比/换手）+ 净值曲线与基准对比
- `reports/compare.png`：多引擎资金曲线对比图
- `reports/backtest_compare.png`：回测净值曲线对比图
- `data/aitrader.db`：SQLite 全量数据（可用 SQLite 工具查看 `accounts` / `trades` / `decisions` / `daily_snapshots`）
- 命令行输出：每次运行打印各账户总资产与累计盈亏

## 免责声明

本软件仅供技术学习与策略研究，不构成任何投资建议。股市有风险，入市需谨慎。
