# AI Quant Arena · 概要设计 (HLD)

| 项目 | 内容 |
|---|---|
| 版本 | 1.3 |
| 日期 | 2026-08-12 |
| 对应 | SRS v1.3 |

## 1. 架构总览

采用**分层架构**，依赖方向自上而下，核心领域逻辑不依赖外部服务：

```
┌─────────────────────────────────────────────────┐
│ 入口层    run.py (CLI) · scripts/ (定时任务部署)  │
├─────────────────────────────────────────────────┤
│ 应用层    batch.py (流程编排) · reporter.py (报表) │
├─────────────────────────────────────────────────┤
│ 领域层    engines/ (决策) · risk.py (风控) · features.py (指标) │
│           portfolio.py (账本+计息+止损) · adjfactor.py (复权)  │
│           attribution.py (归因) · notify.py (告警) · datasource │
├─────────────────────────────────────────────────┤
│ 基础设施  database.py (SQLite) · config.py (配置) · lock.py   │
│           util.py (重试) · logging · .env (密钥)              │
└─────────────────────────────────────────────────┘
```

关键原则：
- **风控与账本为纯函数**：输入状态 → 输出新状态，不读写 IO，天然可测。
- **引擎/数据源可注入**：统一接口，测试时替换为 Fake。
- **状态持久化唯一入口**：所有账本变更经 `database` 层事务提交。

## 2. 模块划分

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `config.py` | pydantic 配置模型、加载 `.env` 与 `config.json` | `load_settings() -> Settings` |
| `datasource.py` | 行情数据源抽象与 akshare 实现 | `DataSource.fetch_daily_bars(symbol, days, exchange, end_date) -> list[Bar]`；`is_trading_day(date) -> bool` |
| `datasource.py` | 政策数据源抽象与财联社实现 | `PolicySource.fetch_macro_news(keywords, max_items) -> list[str]` |
| `engines/base.py` | 决策引擎抽象 | `DecisionEngine.decide(ctx) -> list[Decision]`（ctx 含可选 policy_text） |
| `engines/deepseek.py` | DeepSeek 调用、提示词构造、JSON 解析、降级 | 继承 `DecisionEngine`；`include_policy=True` 时注入政策快讯 |
| `engines/rule.py` | 内置双均线基线 | 继承 `DecisionEngine` |
| `risk.py` | 风控校验（纯函数） | `validate_buy(...) -> AdjustResult` |
| `portfolio.py` | 账本状态与交易执行（纯函数） | `apply_trade(state, trade)`；`refresh_prices`；`apply_cash_interest`（计息）；`apply_stop_rules`（止损/止盈强制卖） |
| `features.py` | 确定性技术指标（纯函数） | `compute_features(bars) -> dict`（MA/RSI/动量/波动率/距高/量比，无前视） |
| `adjfactor.py` | 后复权式调整（分红加回） | `compute_adjusted_bars(bars, dividends)`；`fetch_dividends`（新浪，进程内缓存） |
| `attribution.py` | 归因复盘（纯函数） | `parse_tag` / `attribute_trades`（按 reason 标签聚合已平仓净盈亏）/ `closed_trade_pairs`（历史反馈用） |
| `notify.py` | 告警判定与推送 | `check_alerts`（失败/无成交/回撤）；`send_notify`（webhook，失败不阻塞） |
| `lock.py` / `util.py` | 单实例锁 / 指数退避重试 | `FileLock.acquire()`；`retry_call(func, label)` |
| `database.py` | SQLite 仓储 | `AccountRepo` / `TradeRepo` / `SnapshotRepo` / `DecisionRepo` |
| `batch.py` | 每日流程编排 | `run_batch(date, engine_mode) -> Report` |
| `backtest.py` | walk-forward 回测 | `Backtester.run() -> {snapshots, trades, metrics}`；`compute_metrics` / `compute_benchmark` 纯函数 |
| `reporter.py` | 资金曲线图、周报、回测报表 | `plot_compare(accounts) -> Path`；`plot_backtest_curves` / `build_backtest_report` |
| `run.py` | CLI 入口 | argparse 解析 `--engine/--date/--report` |

## 3. 数据模型（SQLite）

```
accounts(id PK, name, engine_type, initial_capital, created_at)
account_states(account_id PK, state_json, updated_at, last_interest_date)  # last_interest_date = 计息幂等标记
batch_runs(account_id, date, status, created_at, UNIQUE(account_id, date))  # 崩溃重跑幂等（status=done 才算完成）
daily_snapshots(id PK, account_id FK, date, bar_date, source, interest, cash, total_assets, pnl, UNIQUE(account_id, date))
trades(id PK, account_id FK, date, symbol, name, action, price, volume, amount, reason, created_at)
decisions(id PK, account_id FK, date, engine_type, symbol, action, amount, confidence, reason,
          fallback, validation, execution_result, forward_return, prompt_json, raw_output_json, created_at)
bars(id PK, symbol, date, open, high, low, close, volume, UNIQUE(symbol, date))
```

- 多引擎 = 每个引擎一个 `accounts` 行（`engine_type` 区分 `rule` / `ai` / `ai_policy`）。
- `daily_snapshots.bar_date` = 实际行情日期（数据时点审计，NFR8）；`source` = real/replay（回放隔离）；`interest` = 当日货基利息。
- `decisions.confidence/validation/execution_result/forward_return` = 置信度、语义校验、执行结果回填、Rank IC 校准。
- `decisions.prompt_json` / `raw_output_json` 保存喂给引擎的完整行情与引擎原始输出，满足审计/回放（NFR2）。

## 4. 核心流程（每日批处理时序）

```
run_batch(date, force)
  0. 交易日判断（非交易日跳过）＋ 收盘后守卫（<15:00 拒绝当日结算）＋ 数据时点硬校验（bars[-1]==date，陈旧剔除）
  1. 拉取行情：原始 bars + 本地复权 adjusted（双 bars，特征用复权/估值成交用原始）→ 写入 bars 表
  2. 对每个引擎 (rule / ai / ai_policy):
       a. 读取账本状态
       b. 幂等：快照 或 batch_runs=done 则跳过；崩溃遗留 running 无快照可重跑
       c. 构造决策上下文（原始行情 + 复权特征 + 持仓 + 现金 + 可选政策/历史盈亏反馈）
       d. engine.decide(ctx) → decisions（含 confidence、[标签] reason）
       e. 决策落库（含 prompt/raw/validation）
       f. risk 校验 + 可选止损/止盈强制卖出（apply_stop_rules）
       g. execute 执行 → 新状态
       h. 现金生息（last_interest_date 幂等）
       i. 状态落库 + 写 daily_snapshot（含 bar_date/source/interest）+ trades + execution_result 回填
       j. Rank IC 回填（仅实际成交 buy，满 20 交易日窗口）
  3. 告警通知（可选，跨全部引擎判定）＋ reporter 生成对比日报
```

## 5. 关键设计决策

| 决策 | 理由 |
|---|---|
| 双账本并行 | 用同一段行情分别跑 AI 与规则，得到可对比的客观结果（SRS FR2） |
| 决策引擎统一接口 | 新增引擎零成本接入，符合开闭原则 |
| 风控/账本纯函数 | 满足可测试性（NFR1），避免账本计算依赖 IO |
| 数据源注入 | 测试用 Fake 数据，不依赖 akshare 网络（NFR1） |
| 政策引擎可选 | DeepSeek 引擎 `include_policy` 开关：同一模型分“纯价格”与“价格+政策”两账户，隔离变量便于归因（SRS FR12） |
| 密钥走 `.env` | 隔离配置与密钥，防泄露（NFR5） |
| 决策全留痕 | 任何一次买卖都能还原"当时看到什么、为什么这么做"（NFR2） |
| 数据源按日期取数 | `fetch_daily_bars` 支持 `end_date`，回放历史日期只取截至该日行情，杜绝前视偏差（FR10） |
| 交易日判断 | `is_trading_day`：交易日历（akshare）判断，失败降级为仅跳过周末，避免节假日/周末制造假快照（FR13） |
| 同日幂等 | 每日批处理以 (账户, 日期) 快照存在性判重，定时任务 + 启动项兜底同日跑两次不会重复成交；`--force` 强制重跑（FR13） |
| 独立回测库 | 回测写 `data/backtest.db`，不污染每日仿真账本；每次回测前重置账户保证可重复（FR14） |
| AI 响应缓存 | DeepSeek 响应按 (model, prompt) 哈希缓存到 `data/ai_response_cache.json`；prompt 相同（如回测中政策版退化）时共享缓存，重跑不重复计费（FR14） |
| 基准买入持有 | `compute_benchmark`：以区间首日收盘全额买入逐日折算，与策略净值对比归因 alpha/beta（FR14） |
| 回测无前视 | 复用 P0 的 `end_date` 取数，逐日只喂截至当日行情；回测不注入当下政策（FR14） |
| 回测跳过政策版 | 回测无历史政策源，政策版退化为纯价格版；默认跳过避免重复曲线误导，仅 `--engine ai_policy` 显式回测（FR14） |
| 双 bars 复权 | 真实盘拉原始价 + 本地 `compute_adjusted_bars` 生成复权价（分红缓存、零额外网络），特征用复权、估值/成交用原始（N-1） |
| 特征注入 | `features.py` 纯函数算指标注入提示词，替代模型心算；只依赖最近 N 根，无前视（FR15） |
| 置信度 + Rank IC | AI 输出 confidence；门槛过滤；回测/真实盘对实际成交 buy 回填后续收益算 Spearman IC（FR17/18） |
| 市场环境注入 | 可选市场温度行，A/B 后默认开（更稳）（FR19） |
| 历史盈亏反馈 | 可选近 N 笔已平仓交易注入，只含实际成交、无前视（FR20） |
| 止损/止盈 | `apply_stop_rules` 强制卖出（[stop_loss]/[take_profit] 标签）；网格无有效组合默认关（FR21） |
| 现金生息 | 空仓现金按 `cash_interest_rate/252` 逐日计息，`last_interest_date` 幂等，快照记 interest（FR22） |
| catch-up 补跑 | `--catch-up` 从最近快照次日补齐缺失交易日，幂等；部署启动项已接线（FR23） |
| 告警通知 | `notify.py` 跨全部引擎判定失败/无成交/回撤，webhook 推送失败不阻塞（FR24） |
| 数据时点硬校验 | bars[-1]==决策日 才允许交易/估值，陈旧剔除 + 告警，杜绝旧价假当日（NFR8） |
| 政策 15:30 截断 | 只取决策日 15:30 前政策，防收盘后消息 justify 收盘成交（NFR9） |

## 6. 错误处理与降级

- DeepSeek 网络异常 / 超时 / JSON 解析失败 → 该引擎当日记一条 `hold` 决策并标记 `fallback=true`，**不中断**另一引擎与整体流程（NFR3）。
- 单标的行情缺失 → 跳过该标的，其余正常。
- 所有异常均写入日志，流程最终状态由数据库快照保证一致。

## 7. 测试策略

- **单元测试**（pytest）：`risk`、`portfolio`、`rule`、`deepseek 解析`、`config` 校验。
- **Mock 测试**：`FakeDataSource`（固定行情）、`FakeHttpClient`（固定 DeepSeek 响应）。
- **集成测试**：临时 SQLite 上跑完整 `run_batch` 冒烟，断言账本、快照、成交一致。
- **政策测试**：关键词过滤（宏观政策命中、公司公告噪音剔除）、提示词含政策段落、三方引擎账户各自独立。
