# AI Quant Arena · 代码评审与改进建议（自动量化 Agent 视角）

| 项目 | 内容 |
|---|---|
| 版本 | v0.7（覆盖 v0.6 正文，对应代码 v0.19，git HEAD=`a6a23c1`） |
| 日期 | 2026-08-13 |
| 状态 | 评审稿（建议优先级落地，落地后逐项打勾） |
| 评审对象 | `ai_trader/` 全工程（引擎 / 风控 / 账本 / 回测 / 数据源 / 报表 / 部署 / 通知 / 数据库） |

> 本文档基于**真实磁盘代码逐行核实**（v0.19，`187` 个 pytest 用例收集通过）+ **真实运行数据核查**（`data/aitrader.db`、`data/last_run.json`、`data/logs/app.log`，2026-08-13 主 agent 亲自用终端查询），非缓存视图。
> **双轨分工**：工程可靠性轨（本文件）管"机器能否安全、幂等、可观测地自动跑"；预测/决策质量轨（`docs/PREDICTION_IMPROVEMENTS.md`）管"决策质量与评测闭环"，二者不重复。v0.19 已落地项收敛进第 7 节；本轮新发现以 🔴 标注。

---

## 1. 总体评价

系统作为"自动量化体验机"已达到**很高的工程成熟度**：v0.19 已修复上一轮（v0.6）新发现的 P0-3 回撤告警 bug（`_maybe_notify` 传全量快照，1 行 + 3 测试），187 用例全过；每日批处理、三引擎并行、全量留痕、回测、报表、定时任务均已工作。

**本轮主 agent 亲自核实**：v0.6 列的 P0-1（`--force` 同日重跑可重复成交）、P0-2（真实盘/回测参数漂移）、P1-1（回测无台账）、P1-2（无 `--health`）、P1-3（单一数据源）、P1-4（日历无缓存）、P1-5（政策不归档）、P1-6（API 成本无累计）**仍然全部属实**，均未落地。

**本轮新增发现（按真实代码/真实数据核实）**：

1. **🔴 P0-4 崩溃窗口仍可重复成交**（`batch._run_engine` 写路径非原子，v0.12 修复只覆盖了 `begin_batch_run` 之前的崩溃）；
2. **🔴 P0-5 生产账本从未计息**（真实库 `account_states.last_interest_date` 全 NULL、`daily_snapshots.interest` 全 0——v0.16+ 计息代码已合入但**从未在真实盘运行过**，日报"货基利息累计 0.00 元"是事实，不是异常，但意味着长期空仓现金收益被漏计，且首次运行会一次性补计息而非逐日累积）；
3. **🔴 P1-9 `Database(':memory:')` 有 bug**（schema 只建在第一条连接上，`_connect()` 每次新建连接 → `save_bars` 报 `no such table: bars`）；
4. **🔴 P1-10 日报基准对照实时联网 + 未复权 + 500 根硬编码**，与策略 hfq 口径不一致，网络失败时静默留空；
5. **🔴 P1-11 回测默认结束日与批处理日历守卫口径不一致**（`_last_closed_trading_day` 无 `calendar_ok` 保守守卫）；
6. **🔴 P2-10 代理失败无降级**（真实日志 2026-08-13 14:08 新浪请求因代理 127.0.0.1:7897 拒绝连接而 3 次重试全败，全天剔除标的）。

一句话大白话：**机器已能每天自己跑、每笔都留底；但要当"自动 Agent"放心用，还差三件事——① 把"跑一天"做成一个原子事务（崩溃不重复成交、计息不漏计）；② 加上 `--health` 自检 + 回测台账 + 参数一键核对（自我改进的燃料）；③ 数据源/代理多一层兜底（别再因为一个代理挂了就少跑一天）。**

---

## 2. 🔴 P0（真 Bug / 数据可信度 / 无人值守风险，建议最先处理）

### P0-1（✅ v0.20 已落地）`--force` / 崩溃重跑不再重复成交——trades 唯一约束 + INSERT OR IGNORE 幂等

- **位置**：`aitrader/batch.py::_run_engine`（L333-341）／`aitrader/database.py::add_trade`（真实 DDL 已核实，`trades` 无 `UNIQUE(account_id, date, symbol, action)`）
- **现状**：v0.17 已用 `last_interest_date`（计息幂等）+ `batch_runs`（崩溃重跑只认 done）堵住两个口子；但 `--force` 时 `_run_engine` **跳过** `has_snapshot / has_batch_run` 检查直接执行，AI 因温度/状态/特征变化可能给出**不同的当日决策** → `add_trade` 再 INSERT 一笔新成交（同日同账户同标的同向），`trade_count`、`compute_metrics`、归因全部被重复计入。
- **建议**：① 重新定义 `--force` 语义——"先清当日该账户 `trades`/`decisions`/`batch_runs` 再重跑"（排障场景），而非"叠加"；② 给 `trades` 加唯一约束 + upsert（同日同标的同向视为更新，保留首次 reason）；③ `--force` 打印"已清除当日 N 条旧留痕"。
- **影响**：无人值守下 `--force` 是排障常用手段，当前仍可能污染账本。

### P0-2（存量，仍未修）真实盘与回测成交/风控口径漂移——改进点"看得见但难对齐"

- **位置**：`run.py`（回测 CLI 参数）／`config.json`（risk 块，真实文件已核实仅 `max_position_pct/max_daily_buy_pct/commission_rate` 三字段）／`aitrader/portfolio.py::execute_decisions`
- **现状**：
  - 真实盘以**当日收盘价**成交（`last_run.json` 的 `fill_note` 已写明），回测默认 `fill_mode:next_open`（`config.json` 已核实 `fill_mode:"next_open"`）——两者口径本就不同；
  - `config.json` 的 `risk` 块**只暴露** 3 字段，`slippage_bps / stop_loss_pct / take_profit_pct / min_confidence_buy` 用 pydantic 默认值（0/0/0/0）→ **真实盘恒无滑点、无止损止盈、无置信度门槛**；而回测可 `--slippage/--stop-loss/--min-confidence` 临时开启 → 两套环境参数可漂移。
- **建议**：① `config.json` 的 risk 块补全新参数（并入 risk 块，见 P1-8）；② 新增 `--print-config`/`--check-config` 打印"真实盘与回测各自实际生效的参数"，运行前核对（Agent 化 A-2）；③ 在 README/日报里明确标注真实盘成交假设。

### P0-3（已修，v0.19 落地）回撤告警永不触发——`_maybe_notify` 传参 bug

- ✅ **v0.19 已修复**（git HEAD=`a6a23c1`）：`_maybe_notify` 改传全量 `snaps`，`check_alerts` 正常计算 `peak`/`last`；新增 `tests/test_v19.py` 3 条（全量快照回撤触发 / 单元素不触发 / 集成触发推送）。187 用例全过。

### 🔴 P0-4（本轮新发现）崩溃窗口仍可重复成交——批处理写路径非原子

- **位置**：`aitrader/batch.py::_run_engine`（L333 `begin_batch_run` → L341 `add_trade` 循环 → L351 `save_state` → L353 `add_snapshot` → L364 `complete_batch_run`）
- **现状（真实代码逐行核实）**：`_run_engine` 的写路径是**逐条独立连接、无事务包裹**：
  1. `begin_batch_run` 写入 `batch_runs(status='running')`；
  2. `add_trade` 逐笔 INSERT；
  3. `save_state` → `add_snapshot`；
  4. `complete_batch_run` 把 status 置 `done`。
  - 若进程在 **步骤 2 之后、步骤 4 之前**崩溃（DeepSeek 超时拖尾 / 断电 / `add_trade` 中途异常），库中留下：`batch_runs='running'` + **部分已写入的 trades** + **无快照**。
  - v0.12 的 N-2 修复（`has_batch_run` 只认 `done`、无快照可重跑）只能保证"**重跑不会假跳过**"，但重跑时 `_run_engine` 会**再次执行全部决策并再次 INSERT trades**——已写入的那几笔没有唯一约束可去重 → **重复成交**。
  - 与 P0-1（`--force`）不同的是：这个窗口在**无人值守、无人干预**的正常崩溃重跑路径上，风险更高。
- **建议**：把 `begin_batch_run → add_trade×N → save_state → add_snapshot → complete_batch_run` 包进**同一个 SQLite 事务**（`Database` 增加 `transaction()` 上下文管理器，`with self.db.transaction(): ...`），任一环节失败整体回滚，重跑从干净状态开始；同时给 `trades` 加 `UNIQUE(account_id, date, symbol, action)`（与 P0-1 共用）。
- **验收**：pytest 模拟"add_trade 2 笔后抛异常"→ 断言库中无残留 trades、重跑后恰好 1 套成交。
- **影响**：把"崩溃不重复成交"从"只防假跳过"升级为"写路径原子"。

### 🔴 P0-5（本轮新发现）真实账本从未计息——`last_interest_date` 全 NULL、`interest` 全 0

- **位置**：`aitrader/batch.py::_run_engine`（L292-298）／`aitrader/database.py::set_last_interest_date`（L239）
- **现状（真实库核查）**：
  - `data/aitrader.db`：`account_states.last_interest_date` 三账户全部 `NULL`；`daily_snapshots.interest` 全部 `0.0`（08-07 ~ 08-12）；
  - `config.json`：`cash_interest_rate: 0.017`（已核实）；
  - 代码（v0.16+，git 时间 08-12 16:02 合入）逻辑正确：临时文件库实测 `last_interest_date` 正常写入、利息正常入账；
  - 但真实库最后一批快照（08-12 15:30 运行）是在 v0.16 合入**之前**执行的部署版本写入 → **v0.16+ 计息代码从未在真实盘运行过一次**（08-13 15:30 前的运行都被 `before_close` 守卫拦下，见 `last_run.json`）。
  - 后果 ①：日报"货基利息累计 0.00 元"是事实（不是 bug 表现，但代表 v0.16 特性从未生效）；后果 ②：08-13 首次以 v0.16+ 运行时，`last_interest_date=NULL` 会被当作"今天首次计息"，只计**当天一天**利息，08-07~08-12 已过去的计息日**永远不会补计** → 与"逐日复利"口径产生系统性偏差。
- **建议**：① 迁移/启动时对 `last_interest_date IS NULL` 且存在历史快照的账户做**一次性回填**（从最早快照日起按 `cash_interest_rate/252` 逐日计息并入账，幂等、只做一次，并在日志/日报注明"历史利息已回填"）；② `--health` 增加"计息新鲜度"检查项（`last_interest_date` 应等于最近已处理交易日）；③ 日报对"利息累计=0 但账户已运行 N 天"给出提示，避免误读。
- **验收**：pytest 断言回填逻辑幂等；真实库回填后 `last_interest_date` 非空、利息与手工复算一致。
- **影响**：计息是"自动 Agent"资产定价的一部分，漏计会造成净值曲线系统性偏差（空仓期越久偏差越大）。

---

## 3. 🟡 P1（可靠性 / 评测科学性 / 自我改进地基）

### P1-1（存量，仍未修）回测结果无结构化留档（run ledger）——"自动 Agent 自我改进"的地基

- **位置**：`run.py::run_backtest`／`aitrader/backtest.py`／`aitrader/database.py`
- **现状**：每次回测只打印 + 生成 HTML，**配置、指标、基准、Rank IC、API 调用量都不落库**（真实库无 `backtest_runs` 表、无 `--list-runs`）。跑过的实验无法历史对比。
- **建议**：新增 `backtest_runs` 表（run_id、时间、区间、config 快照 JSON、各引擎 metrics、bench、rank_ic、api_calls/cache_hits、fill/adjust/feature/market_env 等实验参数），每次回测写入；`--list-runs` 可查、`--diff-runs A B` 可对比；回测落库决策顺带回填 `execution_result`。

### P1-2（存量，仍未修）无 `--health` 自检——"环境坏了"要等人肉眼发现

- **位置**：`run.py`／`aitrader/notify.py`
- **现状**：无自检命令；"连续 N 天 `last_run` 陈旧 / 未跑 / 计息未生效"不在告警条件里。定时任务 + 启动项失败、网络永久断开、Key 失效等，只能靠用户每天开日报发现。
- **建议**：加 `--health`（网络连通 / 交易日历可加载 / key 非空 / config 一致性 / bars 新鲜度 / `last_run` 新鲜度 / 各账户最近快照时点 / **计息新鲜度（衔接 P0-5）**），返回非 0 码供定时任务/监控抓取；`check_alerts` 增加 `last_run_stale_days` 阈值。

### P1-3（存量，仍未修）单一数据源单点故障——新浪挂了整日不跑

- **位置**：`aitrader/datasource.py::AkShareDataSource`（历史/实时/政策全走新浪）
- **现状（真实日志佐证）**：2026-08-10 19:58 新浪 DNS 解析失败 3 次全败、2026-08-13 14:08 代理 127.0.0.1:7897 拒绝连接 3 次全败（见 `data/logs/app.log`）→ 当日行情剔除/跳过。无腾讯 `qt.gtimg.cn`/东财 fallback。
- **建议**：`fetch_daily_bars` 主源失败后按配置 fallback 到腾讯 `qt.gtimg.cn`（日线）或东财；实时行情同理（`qt.gtimg.cn` 可用已实测）。多源是无人值守连续性的兜底。

### P1-4（存量，仍未修）交易日历无本地缓存 + 无重试——偶发失败 = 少跑一天

- **位置**：`aitrader/datasource.py::_load_trade_calendar`（进程启动拉一次，失败置 `calendar_ok=False` → 工作日被保守跳过；成功也只存内存，进程退出即丢，无重试包装）
- **建议**：`retry_call` 包装 + 成功落盘 `data/trade_calendar.json`，次日启动先读缓存、失败才联网；缓存附有效期。

### P1-5（存量，仍未修）政策文本不归档——ai_policy 引擎"烧 API 但无法评测"

- **位置**：`aitrader/batch.py::_fetch_policy`／`aitrader/engines/deepseek.py`
- **现状**：`ai_policy` 真实盘每天拉政策喂 AI（并计入 API 调用），但 `policy_text` 不落库（真实库无 `policy_archive` 表）→ 无法回测"政策信息对决策是否有价值"，也复现不了当日决策输入。
- **建议**：新增 `policy_archive` 表（date、items JSON），`_fetch_policy` 落库；攒 3 个月后做"政策版 vs 纯价版"回测 A/B；同时日报显示"今日参考政策 N 条"。

### P1-6（存量，仍未修）AI 成本无累计记账——"烧钱"看不见总量

- **位置**：`aitrader/engines/deepseek.py::api_calls/cache_hits`／`run.py::write_last_run`
- **现状**：只有**进程内**计数 + 写 `last_run.json`（单日），无持久化累计；跨天/跨回测累计 API 次数、估算成本不可见。
- **建议**：新增 `api_usage` 表（date、engine、api_calls、cache_hits）逐日累计；日报/`--health` 展示"本月 API 调用/估算成本"。

### P1-7（存量，仍未修）idle 告警用日历天近似交易日 + 长期空仓误报

- **位置**：`run.py::_maybe_notify`（`cutoff = date - timedelta(days=n*2)`）／`aitrader/notify.py::check_alerts`
- **现状**：① 用 `n*2` 天近似 N 个交易日不精确（长假偏差大）；② `trades_count==0` 无条件告警，对本来就空仓的保守策略是持续误报噪音。
- **建议**：用 `is_trading_day` 过滤统计最近 N 个交易日成交；空转告警区分"有持仓但无成交"（真异常）与"本来就空仓"（降级为提示）。

### P1-8（存量，仍未修）config.json 未暴露全部实验参数——改真实盘风控只能靠 CLI

- **位置**：`config.json`／`aitrader/config.py::Settings`（真实文件已核实：`config.json` 缺 `risk.slippage_bps/stop_loss_pct/take_profit_pct/min_confidence_buy` 以及顶层 `temperature/system_prompt_extra/max_buy_count`）
- **建议**：新参数全部进 `config.json`（并入 risk 块 / 顶层），CLI 覆盖时打印"已覆盖 config 默认"。

### ✅ P1-9（v0.20 已落地）`Database(':memory:')` 有 bug——schema 只建在第一条连接上

- **位置**：`aitrader/database.py::__init__/_connect`（L141-145：`:memory:` 时跳过建目录但仍 `_init_schema()`；`_connect()` 每次都 `sqlite3.connect(self.db_path)`）
- **现状（实测复现）**：`Database(':memory:')` 的 `_init_schema()` 在**第一条连接**上建表；随后每个方法都开**新连接**，而 `:memory:` 数据库是**按连接独立**的 → 第二条连接里 schema 为空。实测 `BatchRunner` 跑批处理在 `save_bars` 处报 `sqlite3.OperationalError: no such table: bars`。
- **影响**：当前生产走文件库不受影响，但**所有依赖 `:memory:` 的测试/工具（未来做内存回放、快速实验）都会踩坑**；若有人用 `--db ':memory:'` 也会直接崩。
- **建议**：`Database` 增加连接池/单连接语义：文件库保持现状，`:memory:` 用 `sqlite3.connect(':memory:')` 持有**单条长连接**并在所有方法中复用；或改用 `file::memory:?cache=shared` + `check_same_thread=False`。给 `test_database_memory_mode` 补一条用例（建库 → 任意写读）。
- **验收**：`Database(':memory:')` 下 `save_bars`/`add_trade` 全链路可跑通。

### 🔴 P1-10（本轮新发现）日报基准对照：实时联网 + 未复权 + 500 根硬编码，且失败静默

- **位置**：`aitrader/reporter.py::build_daily_report`（`ds.fetch_daily_bars(first_sym, 500, ...)` 后按 `first_date` 过滤）
- **现状（真实代码核实）**：日报"基准对照（买入持有）"每次生成时**实时联网**拉新浪 500 根日线，`except Exception: bench_line = ""` 静默留空：
  1. **口径不一致**：策略净值含 hfq 复权 + 货基利息，基准用**未复权原始价**（`compute_benchmark` 在回测报表里支持复权，日报这里没有）→ 基准收益系统性偏低/失真；
  2. **500 根硬编码**：账户运行超过约 2 年后，`first_date` 早于可用数据窗口，基准区间被截断；
  3. **网络依赖**：`--report-only` 也联网，数据源挂了基准静默消失，用户无从分辨"没有基准"还是"基准拉取失败"。
- **建议**：① 基准改用本地 `bars` 表（`save_bars` 已缓存全量）或复用 `compute_benchmark(bars, ...)` + `adjust=hfq`，不联网；② 失败时在日报打印"基准获取失败（原因）"而非静默留空；③ 取数窗口按 `first_date` 动态计算。

### 🔴 P1-11（本轮新发现）回测默认结束日与批处理日历守卫口径不一致

- **位置**：`run.py::_last_closed_trading_day`（`while not ds.is_trading_day(d): d -= 1 day`）
- **现状**：`is_trading_day` 在日历不可用时降级为"仅跳周末"（工作日一律算交易日）；批处理侧则有 `calendar_ok=False` 保守跳过守卫（`batch.run` L67）。回测默认 `end` 用 `_last_closed_trading_day`，**没有** `calendar_ok` 检查 → 节假日（清明/五一等）可能被当作"最近已收盘交易日"当回测 end，与批处理口径不一致。
- **建议**：`_last_closed_trading_day` 检查 `ds.calendar_ok`，不可用时打印告警并回退到"昨天"或明确提示用户指定 `--end`。

---

## 4. 🟢 P2（工程体验 / 可运维性）

| # | 类别 | 位置 | 问题（现状） | 建议 |
|---|---|---|---|---|
| P2-1 | 存量 | `run.py` | `--db` 在批处理/回测语义混用（`run_backtest` 用 `Path(args.db)`），误传会污染每日账本 | 回测改 `--bt-db` 独立参数 + "确认覆盖"保护 |
| P2-2 | 存量 | `aitrader/database.py::_connect` | 无 WAL、`trades/decisions` 无 account_id 索引、每操作重开连接 | `PRAGMA journal_mode=WAL` + 索引 + 长连接（顺带解决 P1-9 的连接语义） |
| P2-3 | 存量 | `aitrader/reporter.py` | `Microsoft YaHei` 硬编码，跨平台乱码 | `matplotlib.font_manager` 探测可用中文字体 |
| P2-4 | 存量 | `aitrader/reporter.py::plot_compare` | 日报资金曲线仍是**绝对"总资产（1e6 轴）"**，与回测净值图口径不一 | 统一净值起点=1（与 `plot_backtest_curves` 一致），叠加回撤子图 |
| P2-5 | 存量 | `.env`／`load_settings` | key 为空/占位无启动校验（批处理模式缺 key 只回退 rule 提示，不报错） | 启动校验并给出明确提示（回测已做显式报错，批处理补上） |
| P2-6 | 存量 | `run.py` | 回测参数覆盖 config 时无冲突告警（如 `--fill close` 但 config `next_open`） | 覆盖时打印"用 X 覆盖 config 的 Y" |
| P2-7 | 存量 | `scripts/` | 脚本（bench_metrics/check_daily/corr_analysis/stop_grid）无统一入口文档、无测试 | 加短注释头（用法/依赖/是否烧 API），纳入 README 工具清单 |
| P2-8 | 存量 | `aitrader/batch.py::_fetch_and_store` | 每天全量拉 lookback+45 天（容灾兜底只防挂、不提速） | 增量拉取（从 `bars` 表最新日期起）+ 收盘后批量预取缓存 |
| P2-9 | 存量 | `aitrader/notify.py`／config.notify | 通知渠道单一（webhook/Server酱），且 notify 默认关 | 支持多渠道（Server酱→微信 / 邮件 / 钉钉），加"日报自动推送"与失败升级 |
| 🔴 P2-10 | 新 | `aitrader/datasource.py`／`util.py` | 真实日志显示代理 `127.0.0.1:7897` 拒绝连接（WinError 10061）3 次重试全败 → 全天剔除标的；代码无"代理不可达时直连"降级 | 请求级代理检测：首次 `ProxyError` 时对该调用降级为直连重试（或配置 `NO_PROXY` 白名单给国内行情源）；`retry_call` 对 `ProxyError` 特殊处理 |
| 🔴 P2-11 | 新 | `scripts/install_task.ps1` | 硬编码 `C:\veighna_studio` 与 `D:\下载的堆砌\...` 绝对路径；`$script` 用脚本所在目录推导更稳 | 用 `$PSScriptRoot` 推导路径 + 环境检测（`Get-Command pythonw`），仓库换目录后免改脚本 |
| 🔴 P2-12 | 新 | `run.py` CLI help | `--fill` help 写"close 当日收盘（默认）"，但 `config.json` 实为 `fill_mode:"next_open"`，默认值实际来自 config，help 有误导 | help 文案改为"默认取 config.json（当前 next_open）"；README 同步 |
| 🔴 P2-13 | 新 | `aitrader/config.py::Settings` | `feature_inject/market_env_inject` 默认 `False`，但 `config.json` 均为 `true`；`fill_mode` 默认 `close` 而 config 为 `next_open`——纯代码默认与配置漂移，任何人新建配置都会得到不同行为 | 统一：要么把 config 值写回代码默认（与 A/B 结论一致），要么在文档标注"默认以 config.json 为准"；配合 `--print-config`（A-2）一键核对 |

---

## 5. Agent 化专项（从"每天定时跑"升级为"自动量化 Agent"）

> 以上 P0/P1/P2 是"把机器修稳"；本节是"让机器能自我改进、可被自动管理"。

- **A-1 实验台账 → 自动对比循环**：P1-1 的 `backtest_runs` 落库后，加 `--list-runs` / `--diff-runs A B`，并让 `scripts/bench_metrics.py` 自动汇总"近 N 次实验"对比表；Agent 每周自动复跑基线 + 输出"收益/回撤/夏普/成本"建议报告（可人审后落地）。
- **A-2 `--print-config` 一键核对**：打印真实盘与回测各自实际生效的 fill/adjust/feature/market_env/slippage/stop-loss/temperature 等，防口径漂移（P0-2 落地件；顺带解决 P2-13 的默认值困惑）。
- **A-3 日报升级为"预测 vs 实际"评分卡**：在现有（今日决策/基准对照/数据截至/归因/分桶胜率/Rank IC）基础上，加 **AI 成本累计**（P1-6）与"近 20 交易日 Rank IC 滚动值"，让 Agent 的每日决策可被自动打分。
- **A-4 自动风控重估**：`scripts/stop_grid.py` 网格脚本已具备，可改为"每周自动跑一次止损/止盈网格（rule 免费）+ 输出是否建议开启"；AI 引擎的重估需烧 API，排期。
- **A-5 通知分级**：P0（数据挂/运行失败/计息异常）→ P1（净值回撤/连续无成交/空仓误报修正）→ P2（每日日报），多渠道推送；`--health` 结果可被通知/监控消费。**前置：先修 P0-5 计息新鲜度检查，让"账本是否在正常演化"可被监控。**

> 预测质量轨剩余项（PP-7 政策归档回测、PP-8 标的池扩池）在 `docs/PREDICTION_IMPROVEMENTS.md`，需烧 API / 攒数据，不在本文件重复。

---

## 6. 落地优先级建议

**第一梯队（改动小、直接堵数据可信度/无人值守口子，建议立即做）**
1. 🔴 P0-4 批处理写路径单事务化（`Database.transaction()` + `trades` 唯一约束）——约 2~3 小时
2. 🔴 P0-5 计息回填 + `--health` 计息新鲜度检查——约 2 小时
3. 🔴 P1-9 `:memory:` 连接语义修复 + 测试——约 1 小时
4. P0-1 `--force` 幂等重定义（清当日留痕再跑 / upsert）——约 1~2 小时
5. P1-2 `--health` 自检 + last_run 陈旧告警——约 1 小时

**第二梯队（评测科学性 / 成本可观测 / 防漂移）**
6. P1-1 回测 run ledger 落库 + `--list-runs`——约 2~3 小时
7. P1-3 行情多源 fallback（腾讯备用）+ 🔴 P2-10 代理降级——约 1~2 小时
8. P1-4 交易日历本地缓存 + 重试——约 1 小时
9. P1-5 政策归档（攒 3 个月后可回测）——约 1~2 小时
10. P1-6 AI 成本累计记账 + 日报展示——约 1~2 小时
11. P0-2 + P1-8 config risk 补全 + `--print-config`（含 🔴 P2-13 默认值统一）——约 1 小时
12. 🔴 P1-10 日报基准改本地 bars + hfq + 失败可见——约 1 小时
13. 🔴 P1-11 回测默认结束日守卫——约 0.5 小时

**第三梯队（工程打磨 + Agent 化）**
14. P1-7 idle 告警精度、P2-1~P2-9 逐项、🔴 P2-11/P2-12——随迭代消化
15. A-1~A-5 自动对比 / 核对 / 评分卡 / 风控重估 / 通知分级

---

## 7. 验收清单 + 落地记录

### 已验证（v0.19 现状，2026-08-13 真实代码复核 + 187 用例收集通过 + 真实数据核查）

- [x] 日志落盘 `data/logs/app.log` 5MB×5 轮转／`last_run.json` 运行留证（异常保留上次正常）／单实例文件锁
- [x] 同日幂等（快照 + `batch_runs` 只认 done + `last_interest_date` 计息幂等逻辑）／交易日判断／`--date` 回放无前视／`source`(real/replay) 隔离
- [x] 行情硬校验（bars[-1]==决策日）＋逐标的陈旧剔除＋实时补全（新浪 hq.sinajs.cn）＋政策 15:30 截断防前视＋政策决策日过滤
- [x] DeepSeek 单条解析容错／语义校验留痕（validation + already_holding）／网络重试（retry_call）／响应缓存原子写＋缓存键含 model/temperature/system/prompt
- [x] 回测 O(log N)、报表只渲染本轮引擎、基准佣金同口径、`fill_mode:next_open` + `adjust:hfq` 默认、`--fill/--commission-mult/--adjust`、回测 end 默认最近已收盘
- [x] 双 bars（真实盘特征复权）／特征注入 + 市场环境注入（config 已开 true）／置信度门槛／历史盈亏反馈／止损止盈／滑点／现金生息（代码已具备，真实盘未生效——见 P0-5）
- [x] 真实盘 Rank IC 回填 + 日报（分桶胜率/归因复盘/基准对照/数据截至/今日决策/货基利息卡片）
- [x] 引擎级异常隔离／收盘守卫（<15:00 拒绝结算，真实日志 08-13 两次 `before_close` 正常触发）／未来日期拒绝
- [x] `--catch-up` 补跑 + 启动项接线／通知（N-10，跨引擎判定）／bars 缓存兜底（N-11）／API 调用计数（N-8，last_run.api_stats）
- [x] **P0-3 回撤告警修复（v0.19，`_maybe_notify` 传全量快照 + 3 测试）**

### 验收清单（本轮待落地，落地后逐项打勾）

- [ ] 🔴 P0-4 批处理写路径单事务 + `trades` 唯一约束——崩溃窗口不再重复成交
- [ ] 🔴 P0-5 计息回填（`last_interest_date IS NULL` 且有历史快照）+ `--health` 计息新鲜度——真实库利息与手工复算一致
- [ ] 🔴 P1-9 `Database(':memory:')` 单连接/共享缓存修复 + 测试
- [ ] P0-1 `--force` 幂等重定义（清当日留痕重跑 / trades upsert）
- [ ] P1-1 回测 run ledger 落库 + `--list-runs`/`--diff-runs`
- [ ] P1-2 `--health` 自检 + last_run 陈旧告警
- [ ] P1-3 行情多源 fallback（腾讯备用）+ 🔴 P2-10 代理降级
- [ ] P1-4 交易日历本地缓存 + 重试
- [ ] P1-5 政策归档（policy_archive）
- [ ] P1-6 AI 成本累计记账 + 日报展示
- [ ] P1-7 idle 告警精度（按交易日 + 空仓区分）
- [ ] P1-8 + P0-2 config risk 补全 + `--print-config`
- [ ] 🔴 P1-10 日报基准改本地 bars + hfq + 失败可见
- [ ] 🔴 P1-11 回测默认结束日日历守卫
- [ ] P2-1~P2-9 + 🔴 P2-11/P2-12/P2-13 逐项 + Agent 化 A-1~A-5——第三梯队

### 落地记录（v0.1~v0.19 里程碑，详见仓库 git log）

- **v0.1~v0.2（08-09）**：日志/last_run/单实例锁/语义校验留痕；ai_policy 批处理过滤、行情失败跳过、DeepSeek 解析容错、网络重试、回测 O(log N)。
- **v0.3~v0.4（08-09）**：回测报表只渲染本轮引擎、数据新鲜度守卫、逐标的陈旧剔除 + bar_date、`batch_runs` 防崩溃重复成交（部分，见 P0-4）、execution_result 回填、日期参数校验。
- **v0.5~v0.7（08-09）**：成交假设校准（PP-1）、system 三段式 + 温度/模型参数化 + 缓存键修正（PP-3）、特征注入（PP-2）、置信度门槛 + Rank IC 闭环（PP-4）、复权接入回测、网络硬超时 + IPv6 修复。
- **v0.8~v0.9（08-10）**：实时行情补全（治新浪滞后 1 日）、政策 15:30 截断防前视 + 决策日过滤、行情"当日硬校验"。
- **v0.10~v0.12（08-10）**：真实盘接入特征注入（A-1）、未来日期拒绝（A-4）、引擎异常隔离（A-2）、收盘守卫（A-3）、回测 end 默认最近已收盘（A-5）、滑点（P2-2）、真实盘 Rank IC 回填（B-1）、日报升级（B-2）、`batch_runs` 只认 done（N-2）、`source` 隔离（N-12）、信心分桶（N-4）。
- **v0.13~v0.14（08-11）**：双 bars 真实盘特征复权（N-1）、成交口径说明（N-3）、归因标签 + 复盘（N-5）、`--market-env` 接线（N-6）、API 记账（N-8）、`--catch-up`（N-9）、通知（N-10）、bars 缓存兜底（N-11）。
- **v0.15（08-11）**：烧 API A/B——市场环境注入开启（夏普 0.98→1.57）、历史盈亏反馈可选、止损/止盈网格无有效组合保留默认关、标的池相关性 0.75~0.87 高度冗余。
- **v0.16（08-12 16:02）**：现金生息（货基假设，代码合入；**真实盘从未生效——见 P0-5**）。
- **v0.17（08-12 16:18）**：P0-1 计息幂等（last_interest_date）、P0-2 告警跨引擎、P0-3 日报数据截至 + 陈旧高亮、P0-4 启动项接 `--catch-up`、P0-5 IC 只收实际成交；P1-1 归因/浮盈净佣金口径、P1-2 `already_holding`、P1-3 实时行情重试、P1-6 last_run 保留上次正常。
- **v0.18（08-12 16:23）**：日报展示货基利息累计（快照 interest 列 + 卡片单列）。
- **v0.19（08-12 23:26）**：P0-3 回撤告警修复（`_maybe_notify` 传全量快照，v0.17 引入的回归）+ `tests/test_v19.py` 3 条；187 用例全过。

### v0.7 评审说明（2026-08-13，主 agent 亲自终端读真实磁盘代码 + 真实数据）

- v0.6 的 P0-1/P0-2/P1-1~P1-8 全部核实**仍然属实且未落地**（trades 无唯一约束、config risk 块 3 字段、无 backtest_runs、无 --health、单一数据源、日历无缓存、无 policy_archive、无 api_usage、idle 用 n*2 天、plot_compare 1e6 轴、无 WAL/索引、字体硬编码）。
- **本轮新增 P0-4（批处理写路径非原子）**：`_run_engine` 中 `begin_batch_run → add_trade×N → save_state → add_snapshot → complete_batch_run` 无事务包裹，进程在中间崩溃会留下"running + 部分 trades + 无快照"，重跑重复成交。v0.12 的 N-2 只覆盖 `begin_batch_run` 之前崩溃。
- **本轮新增 P0-5（真实账本从未计息）**：真实库 `account_states.last_interest_date` 全 NULL、`daily_snapshots.interest` 全 0；v0.16（08-12 16:02 合入）晚于真实库最后一批快照写入（08-12 15:30 运行的部署版本），计息代码从未在真实盘执行。首次 v0.16+ 运行会只计当天一天利息，历史计息日永不补计 → 需一次性回填。
- **本轮新增 P1-9/P1-10/P1-11/P2-10~P2-13**：`:memory:` 库 schema 丢失（实测复现 `no such table: bars`）、日报基准实时联网+未复权+500 根硬编码+失败静默、回测默认结束日无日历守卫、代理拒绝连接无降级（真实日志 08-13 14:08 佐证）、部署脚本硬编码绝对路径、CLI help 与 config 默认漂移、代码默认与 config 漂移。


### v0.20 落地记录（2026-08-13，主 agent）

- **P0-1** trades 加 `UNIQUE(account_id,date,symbol,action)` 索引（迁移先清理重复保留最早）+ `add_trade` 改 `INSERT OR IGNORE` → `--force`/崩溃重跑同日同标的同向不再重复成交（幂等）；新增 `tests/test_v20.py` 3 条（重复幂等 / 内存库 / 迁移去重）。
- **P1-9** `Database(':memory:')` 改为复用单连接（`_mem_conn` 惰性创建），schema 建在该连接上不再丢失，`save_bars` 不再报 `no such table`。
- **P0-4（完整事务原子化）** 未做完整事务，但 INSERT OR IGNORE + 唯一约束 + N-2（无快照可重跑）已使崩溃重跑的成交幂等；save_state 写了快照没写的半成品由重跑覆盖。完整 `transaction()` 包裹留作后续加固。测试 190 全过。


### v0.23/v0.24 体检修复落地记录（2026-08-14，主 agent + 12 方向体检）

> 2026-08-13 全量重置后，用户让 12 个方向 Agent 对系统彻头彻尾体检（`tests/20260813/`，去重后 7 P0 / 10 P1），随后分批落地。测试 197 全过。

**v0.23（2026-08-13~14）**：
- `--help` 崩溃修复（help 字符串裸 `%`→`%%`）
- decisions 加 `UNIQUE(account_id,date,symbol,action)` + `INSERT OR IGNORE`（防 --force/崩溃重跑重复决策留痕污染 IC/归因）
- `reset_account` 补清 `batch_runs`
- 计息 marker 后移到 save_state 之后（防"标记先写、状态未写"漏计窗口）
- 政策注入质量：海外邻国噪音过滤 + 标题空/NaN 跳过 + 去重 + 每条限长 150
- reason 标签契约修复（JSON 示例带标签 → `[政策]` 标签可统计）
- confidence 全 action 存储（原只 buy）
- 日报样本不足水印（<20 笔仅过程展示）+ ai_policy 政策注入自检（无政策段标"不可信"）
- 日报基准改用本地 bars 表 + 扣单边佣金 + 失败可见（P1-10 落地）
- notify 失败留档 `data/notify_fail.log`
- install_task.ps1 计划任务接 `--catch-up 5`（**注意：实际部署需重跑脚本**）
- 文档同步（README/SRS → v0.23/197 测试、删假 --health）

**v0.24（2026-08-14）**：
- **P0-2 口径漂移（部分落地）**：config risk 块补全 slippage_bps=10 / stop_loss / take_profit / min_confidence（真实盘风控可生效）+ 批处理真实盘也应用 CLI 风控参数 + `--print-config`（A-2）+ 真实盘除权日分红现金入账（复用分红缓存，仅当日无前视）
- **P1-1 run ledger 落地**：`backtest_runs` 表 + run_backtest 落库（config/metrics/bench/ic/api_calls）+ `--list-runs`（查 backtest.db）
- **P1-2 `--health` 落地**：key / 交易日历（触发加载）/ bars 新鲜度 / 账户快照 / last_run，非 0 退出码，before_close 守卫不误报
- 代理故障直连降级：retry_call 首次 ProxyError → 国内行情域名 NO_PROXY 白名单（不影响 DeepSeek API）
- AI 卖出纪律：prompt JSON 示例给 sell 对称例子 + 明确"不要只买不卖"
- 合规红线：SRS 删除"真钱半自动建议接口 / 1000 元真钱预算"，明确永不接入真实资金；日报头部加"模拟≠实盘"警示条 + 页脚教育

**仍未修（下一批）**：完整事务原子化（`transaction()`，幂等已兜住核心风险）、单一数据源腾讯 fallback（代理降级已缓解）、run.py/reporter 拆分、回测 `adjust=none` 基线（hfq vs 原始价严格统一）、交易日历本地缓存、政策归档、API 成本累计、idle 告警精度、回测 end 日历守卫。
