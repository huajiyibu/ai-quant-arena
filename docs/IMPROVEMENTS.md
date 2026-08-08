# AI 自动交易体验机 · 代码评审与改进建议

| 项目 | 内容 |
|---|---|
| 版本 | 0.4（覆盖 v0.3 正文，保留历史落地记录） |
| 日期 | 2026-08-09 |
| 状态 | 评审稿（待确认优先级后落地） |
| 评审对象 | `ai_trader/` 全工程（引擎 / 风控 / 账本 / 回测 / 数据源 / 报表 / 部署） |

> 本文档基于当前代码逐行阅读 + 全量 77 个 pytest 用例通过（本轮 2026-08-09 实测）验证。v0.1/v0.2/v0.3 已落地项直接进入第 6 节"已验证"清单；已提未做项标注"存量，未落地"；本轮新增 P0-3/P0-4（v0.3 已落地功能的**残留缺口**）、P1-7~P1-9、P2-14~P2-16。每条建议均指向具体文件与函数，便于落地与验收。

---

## 1. 总体评价

系统作为"AI 决策能力评测体验机"已达合格工程水平（承接 v0.3 结论，本轮复核仍成立）：

- ✅ 分层清晰、依赖可注入、核心逻辑可单测（77 个 pytest 用例全过）。
- ✅ 全量留痕：决策输入行情 + 原始输出均入库，任何一笔可回溯。
- ✅ v0.3 已落地并复核通过：回测报表只渲染本轮引擎（`reporter.py` 的 `engine_types` 过滤）、行情"全量陈旧"守卫 + `calendar_ok`、无 key 回测 AI 直接报错、AI 缓存原子写、`resp.json()` 异常留痕、无政策引擎跳过拉政策、回测前打印预计 AI 调用次数。

距"无人值守、结果可当真"仍有三块短板（本轮重点）：

1. **P0-2 的两个残留口子未闭合**（P0-3 / P0-4）——新鲜度守卫只挡"所有标的同时陈旧"，单标的陈旧仍会用旧价成交；交易日历失败时"工作日节假日"仍被当成交易日照常成交；
2. **账本写入非事务**（P1-7）——决策/成交/状态/快照分步落库，中途崩溃次日可能重复成交；
3. **决策"执行结果"不回填**（P1-8）+ **网络无硬超时**（P1-9）——归因统计做不实、定时任务可能挂死。

一句话大白话：**这台机器现在已经能规规矩矩每天自己跑、每笔决策都留底；但要"把结果当真"，还差最后一口气——行情哪怕只有一个标的没更新（或日历断了的节假日），它还会拿旧价格假装今天成交了；而且它记账是分步写的，万一写一半断电，第二天可能重复买卖。**（v0.3 那句"回测报表混旧曲线"已修，这里聚焦它没修完的"数据新鲜度"另一半。）

---

## 2. 🔴 P0（真 Bug / 数据可信度，建议最先处理）

### P0-3 数据新鲜度守卫只覆盖"所有标的同时陈旧"——单标的陈旧仍会用旧价成交（v0.3 P0-2 残留缺口）

- **位置**：`aitrader/batch.py::run`（`STALE_DAYS` 守卫）／`aitrader/portfolio.py::execute_decisions`（`prices` 直取 `bars[-1].close`）／`aitrader/database.py::add_snapshot`（快照无 bar 日期）
- **问题（现状 + 为什么是问题）**：v0.3 落地的新鲜度守卫是 `all((date.date() - d).days > STALE_DAYS for d in latest_dates)`——**只有所有标的**都陈旧超过 5 天才跳过。若仅**一个标的**数据源断更 / 停牌 / 该标的分片拉取异常（其余正常），`all()` 为 False → 照常交易：该标的用**旧价**做当日估值并可能按其旧价成交，写入"当日"快照。这仍是与 v0.2 已修"行情拉空"同级的**静默坏数据**。另外守卫容忍的 1~5 天 T+1 滞后没有把"实际使用的 bar 日期"写进快照，事后审计无法区分"今天"与"上一交易日"的估值。
- **建议**：① 改为**逐标的**检查：任一标的陈旧 >5 天 → 当日剔除该标的（不进 `prices`，持仓不重估、针对它的决策按无价跳过）并显著告警；② 或更保守：任一标的陈旧即整日跳过（与现有全量跳过逻辑统一）；③ `daily_snapshots` 加 `bar_date` 列（或 `state_json` 记录各标的估值日）供审计。**需拍板**：如何区分"合法陈旧"（停牌/长期无成交）与"数据源断更"——建议陈旧阈值可配置，停牌标的单独豁免并记录原因。
- **影响**：堵住"单标的旧价假快照"；改动集中在 `batch.py` + schema + 单测，风险低。
- **类别**：① 真 Bug（静默坏数据）。

### P0-4 交易日历失败时"工作日节假日"仍被当成交易日照常成交（v0.3 P0-2 残留缺口）

- **位置**：`aitrader/datasource.py::is_trading_day`（降级 `date.weekday() < 5`）／`aitrader/batch.py::run`（日历失败告警的位置）
- **问题**：交易日历拉取失败 → `_TRADE_CALENDAR_OK=False`，`is_trading_day` 降级为"仅跳过周末"。此时若当天是**工作日节假日**（清明/五一/端午等 1~3 天），会被当成交易日：akshare 返回**上一交易日的旧 K 线**（非空，不触发行情拉空保护），5 天新鲜度守卫对 1~3 天滞后也放行 → 照常决策成交，写入"节假日日期"的快照与成交。更关键的是，v0.3 加的"日历失败告警"写在 `batch.py::run` 的 `if not self.data_source.is_trading_day(date)` 分支**内部**——降级模式下非交易日只有周末，所以告警**只在周末触发**，恰好覆盖不到这个危险的节假日场景。
- **建议**：① `batch.run` 中当 `calendar_ok is False` 且当天是工作日时，**无论是否判定为交易日都显著告警**（写入 `last_run.json` 告警，不只 logger）；② 更保守：日历失败时当天直接跳过交易（或要求显式 `--force` 才跑）；③ 单测：日历失败 + 工作日 + 旧 K 线（滞后 1~2 天）→ 断言跳过/告警。
- **影响**：彻底闭合 P0-2 的日历降级口子。**需拍板**保守度（跳过 vs 告警后继续）。
- **类别**：① 真 Bug（静默坏数据）。

---

## 3. 🟡 P1（可靠性 / 评测科学性）——本轮新增

### P1-7 每日批处理"决策→成交→状态→快照"分步落库非事务（新发现）

- **位置**：`aitrader/batch.py::_run_engine`（`add_decision` → `add_trade` → `save_state` → `add_snapshot` 顺序）／`aitrader/database.py`（每个方法各开一个连接、`with ... as conn` 自动提交）
- **问题（现状 + 为什么是问题）**：四步各自独立事务、无原子性。若进程在 `add_trade` 之后、`add_snapshot` 之前崩溃（断电/被杀），下次重跑 `has_snapshot` 为 False → 重新决策；此时 `account_states` 仍是**旧状态**（无该持仓）→ 可能重复买入/重复卖出，账本被污染，同日幂等兜不住。无人值守长期运行下概率虽小，但属可复现的账本一致性缺陷。
- **建议**：① 把单引擎的决策+成交+状态+快照包进**单个 SQLite 事务**（`Database` 提供事务上下文 / `batch` 传入共享连接）；② 或先写"当日已处理"标记行（如 `batch_run` 表）再执行业务写，崩溃后跳过；③ 单测：在 `add_trade` 后注入异常，断言重跑不重复成交。
- **影响**：崩溃场景下账本仍一致；改动集中在 `database.py`/`batch.py`。
- **类别**：② 优化（长期可靠性）。

### P1-8 决策表不回填"执行结果"——风控拒绝 / 价格缺失 / 金额截断无法统计（新发现）

- **位置**：`aitrader/portfolio.py::execute_decisions`（风控拒绝 `continue`、价格缺失 `continue`）／`aitrader/database.py::add_decision`（无执行结果列）／`aitrader/risk.py::validate_buy`
- **问题**：语义校验（`deepseek._validate`）结果写进 `decisions.validation`，但"风控拒绝 / 价格缺失 / 实际成交额 / 被截断"没有回填。一条 `validation="ok"` 的 buy 决策可能被 `validate_buy` 静默拦掉或砍额，决策表看不出——无法统计"模型被风控拦了多少、为什么"，P1-4（存量）设想的"乱写率 / 决策差异归因"也做不出来。
- **建议**：`execute_decisions` 返回 per-decision 结果（`executed` / `risk_rejected:原因` / `no_price` / `capped:请求→实际`），回填 `decisions` 新列 `execution_result`；报表增加"决策→执行"漏斗视图（与 P1-4 互补）。
- **影响**：评测归因从"看图说话"变可统计。
- **类别**：② 优化（评测科学性）。

### P1-9 网络调用无硬超时、批处理无总截止——网络半死时定时任务可能挂死（新发现）

- **位置**：`aitrader/datasource.py::fetch_daily_bars` / `fetch_macro_news`（akshare 调用未设 timeout）／`aitrader/engines/deepseek.py::_call`（`timeout=90`）／`aitrader/util.py::retry_call`／`run.py::_run`
- **问题**：① akshare 底层 requests **默认无 timeout**（**需验证**其内部是否传 timeout），网络半死（SYN 挂起不抛异常）时调用可无限阻塞，`retry_call` 只在**抛异常**时重试、对挂起无效 → 定时任务卡死、当日无产出；② DeepSeek `timeout=90` × 3 次重试，最坏 ~4.5 分钟/引擎，两个 AI 引擎近 9 分钟，远超 NFR4（单次批处理 ≤60s）且无任何守卫。
- **建议**：① 给 akshare 调用套显式 timeout（如 20~30s，**需调研**其封装方式）；② 批处理设总 deadline（如 240s），超时按"当日跳过"处理并写 `last_run` 告警；③ timeout/retry 参数进 config（呼应存量 P2-5）。
- **影响**：无人值守不挂死；改动集中在 `util.py`/`datasource.py`/`run.py`。
- **类别**：② 优化（可靠性）。

---

## 4. 🟢 P2（工程体验 / 可运维性）

### 本轮新增

| # | 类别 | 位置 | 问题（现状） | 建议 |
|---|---|---|---|---|
| P2-14 | 新发现 | `aitrader/risk.py::validate_buy`（`amount = min(...)` 静默截断）／`aitrader/portfolio.py::execute_decisions`（`trade.reason = d.reason`） | 模型请求 90% 被风控砍到 30%，成交原因仍是模型原话，报表/决策表看不出"请求额 → 成交额"，审计与评测解释性差 | `validate_buy` 返回 requested/capped 信息，`trade.reason` 前缀追加 `[风控截断 900000→300000]`，或并入 P1-8 的 `execution_result` |
| P2-15 | 新发现 | `run.py::run_backtest`（`bench_cfg = settings.symbols.get(bench_symbol)`，None 时静默空） | `--benchmark` 传未知代码时回测正常结束但**无基准线**，无任何提示 | 未知基准代码直接报错退出（与已落地的 P2-9 同风格） |
| P2-16 | 新发现 | `run.py::main`（`--date/--start/--end` 直接 `strptime`） | `--date garbage` 抛 ValueError 栈进日志，提示不友好 | argparse `type=` 用 `datetime.fromisoformat` 校验，非法值给友好错误 |

### 存量，未落地（v0.1/v0.2/v0.3 已提，本轮复核仍在）

| # | 位置 | 问题（现状） | 建议 |
|---|---|---|---|
| P1-1 | `backtest.py::Backtester.run`／`portfolio.py::execute_decisions` | 收盘价成交偏乐观（15:30 决策按当日收盘价成交，实盘做不到） | 需拍板：次日开盘价成交 or 报表标注"乐观上界" |
| P1-2 | `datasource.py::AkShareDataSource.fetch_daily_bars` | 行情未复权，分红除息跳空失真（**需调研**新浪/东财前复权参数） | 接入复权或 README 注明 |
| P1-3 | `engines/deepseek.py::_build_prompt` | 提示词只喂 20 个收盘价，模型要心算均线/涨跌幅/波动率 | 注入 MA/RSI/波动率/距高低点等特征 |
| P1-4 | `batch.py::_fetch_policy`（政策不落库）／`reporter.py` | 政策版无法归因、无法历史回测 | 新增 `policy_archive` 表存档 + 决策差异表 |
| P1-5 | `risk.py::validate_buy`／`portfolio.py::execute_decisions` | 无止损/无回撤熔断/无移动止损 | 自小到大：单标的止损 → 回撤熔断 → 移动止损 |
| P1-6 | `run.py::run_backtest` | AI 回测成本上限未做（已落地"预计调用次数提示"） | `--max-calls` 上限或抽样模式、缓存断点续跑 |
| P2-1 | `datasource.py`／`backtest.py`／`batch.py` | akshare 单点；`bars` 表**只写不读**（`get_bars` 无调用点，已复核） | `fetch_daily_bars` 先查 `bars` 表缺的再补；多数据源 fallback |
| P2-2 | `reporter.py::plot_compare` | 日报对比图仍绝对"总资产（1e6 轴）" | 与回测图统一净值起点=1 + 逐笔明细 + 乱写率视图 |
| P2-3 | `reporter.py`（两处 rcParams） | `Microsoft YaHei` 硬编码，跨平台乱码 | `matplotlib.font_manager` 探测字体 |
| P2-4 | `database.py::_connect` | 无 WAL、无索引、`save_bars` 逐条 INSERT、每操作重开连接 | `PRAGMA journal_mode=WAL`、加索引、`executemany`、长连接 |
| P2-5 | `config.json`／`config.py::Settings` | `max_buy_count` 等默认值未全部暴露到 config.json | 止损/熔断/时滞/重试/超时等新参数全部进 config |
| P2-6 | `run.py::_run` | 失败只在 `last_run.json`/日志 | 主动通知（企业微信 / Server酱 / 邮件） |
| P2-7 | `run.py` | `--db` 批处理/回测语义混用易误覆盖 | 回测改 `--bt-db` + 确认覆盖保护 |
| P2-8 | `.env`／`config.py::load_settings` | key 为空/占位/异常无启动校验 | 启动校验并给明确提示 |
| P2-12 | `backtest.py::compute_benchmark` | 基准无手续费，与带成本策略对比略偏袒基准 | 需拍板：基准折算佣金 or 报告标注"基准未计成本" |

---

## 5. 落地优先级建议

**第一梯队（半小时级，堵住 v0.3 未闭合的数据可信度口子，建议立即做）**
1. 逐标的陈旧剔除/整日跳过 + 快照记录 `bar_date`（P0-3）
2. 日历失败下工作日显著告警 / 当日跳过（P0-4）

**第二梯队（账本一致性 / 评测科学性，涉及拍板）**
3. 单引擎写账事务化（P1-7）
4. 决策执行结果回填 `decisions.execution_result`（P1-8）
5. 网络硬超时 + 批处理总截止（P1-9）

**第三梯队（工程体验 / 存量推进）**
6. P2-14~P2-16 逐项
7. 存量 P1-1~P1-6、P2-1~P2-8、P2-12 按旧计划推进（P1-1/P1-2 需拍板与调研）

---

## 6. 验收清单 + 落地记录

### 已验证（v0.1/v0.2/v0.3 已落地，2026-08-09 代码复核 + 77 用例全过确认）

- [x] 日志落盘 `data/logs/app.log` 5MB×5 轮转（`run.py::setup_logging`）
- [x] `data/last_run.json` 运行留证（`run.py::write_last_run`）
- [x] 单实例文件锁（`aitrader/lock.py::FileLock`，Windows msvcrt / Unix fcntl）
- [x] AI 语义校验写入 `decisions.validation` + 旧库迁移（`deepseek._validate` / `database._init_schema`）
- [x] `EngineType` 含 `ai_policy`（`models.py`）
- [x] `--engine ai_policy` 每日批处理正确过滤（`run.py::select_daily_engines`，test_v02 覆盖）
- [x] 行情拉取失败当日跳过交易 + `last_run` 告警（`batch.run` 返回 `_warning`，test_v02 覆盖）
- [x] DeepSeek 单条解析容错 + `amount` 字符串兼容（`deepseek._parse` / `_to_float`）
- [x] DeepSeek / akshare 网络重试（`util.retry_call`，接入行情/政策/AI 三处）
- [x] 回测 O(log N) 日期索引（`backtest.py` bisect）
- [x] 回放无前视（`fetch_daily_bars` 加 `end_date`）／ 交易日判断 ／ 同日幂等
- [x] walk-forward 回测 + 指标 + 基准（`backtest.py`）
- [x] AI 响应缓存（`deepseek._call`，按 prompt 哈希，ai/ai_policy 共享）+ 原子写（`run.py` os.replace）
- [x] P0-1 回测报表按 `engine_types` 只渲染本轮引擎（`reporter.py` 两函数 + `run_backtest` 传参，test_v03 覆盖）
- [x] P0-2 数据新鲜度守卫（`batch.py` STALE_DAYS 全量陈旧跳过，test_v03 覆盖）+ `calendar_ok` 属性
- [x] P2-9 无 key 回测 AI 直接报错退出（`run_backtest`）
- [x] P2-11 `resp.json()` 非 JSON 原始文本留痕（test_v03 覆盖）
- [x] P2-13 无 include_policy 引擎跳过拉政策（test_v03 覆盖）
- [x] P1-6 回测前打印预计 AI 调用次数

### 验收清单（v0.4 待落地，落地后逐项打勾）

- [x] 任一标的陈旧 >5 天即剔除该标的并告警（不整日跳过），其余标的正常交易；`daily_snapshots` 记录 `bar_date`—— P0-3
- [x] 交易日历不可用 + 工作日 → 保守跳过交易并返回 `calendar_unavailable` 告警（--force 可强制）—— P0-4
- [x] 写账防重复：`batch_runs` 标记（begin/complete），崩溃后重跑据标记跳过不重复成交（含单测）—— P1-7（以标记方案实现，效果等价于单事务目标）
- [x] `decisions` 新增 `execution_result`，风控拒绝/价格缺失/请求→实际成交额可统计—— P1-8 + P2-14
- [ ] akshare 显式 timeout + 批处理总截止—— P1-9（需调研 akshare 封装，暂缓）
- [x] 风控截断在留痕中体现“请求额→成交额”—— P2-14（并入 execution_result）
- [x] `--benchmark` 未知代码直接报错—— P2-15
- [x] `--date/--start/--end` argparse 校验，非法值友好报错—— P2-16

> 落地记录（2026-08-09，v0.4）：完成 P0-3（batch 逐标的陈旧剔除 + `daily_snapshots.bar_date`）、P0-4（日历不可用工作日保守跳过）、P1-7（`batch_runs` 表 begin/complete/has 标记，崩溃重跑不重复成交）、P1-8 + P2-14（`execute_decisions` 返回三元组 `execution_results`，回填 `decisions.execution_result`，含“请求→实际”截断）、P2-15（未知基准报错）、P2-16（日期参数 `_date_type` 校验）；新增 `tests/test_v04.py`（7 用例），全量 83 用例通过；P1-9 暂缓（需调研 akshare timeout 封装与批处理总截止实现）。

### 落地记录

- **v0.1（2026-08-09，已落地）**：日志落盘 + `last_run.json`、单实例锁 `aitrader/lock.py`、AI 语义校验写入 `decisions.validation`、`EngineType` 补 `ai_policy`；新增 `tests/test_reliability.py`（9 用例）。
- **v0.2（2026-08-09，已落地）**：`ai_policy` 批处理过滤、行情失败当日跳过、DeepSeek 解析容错、网络重试、回测 O(log N)；新增 `tests/test_v02.py`（12 用例），全量 72 用例通过。
- **v0.3（2026-08-09，已落地）**：P0-1 回测报表按 `engine_types` 过滤、P0-2 数据新鲜度守卫（全量陈旧）+ 日历失败告警、P2-9 无 key 回测报错、P2-10 AI 缓存原子写、P2-11 JSON 异常留痕、P2-13 无政策引擎跳过拉政策、P1-6 回测前预计调用次数提示；新增 `tests/test_v03.py`（5 用例），全量 77 用例通过。
- **v0.4（本轮评审，2026-08-09）**：新增 P0-3（单标的陈旧仍成交）、P0-4（日历失败下工作日节假日照常成交）——均为 v0.3 P0-2 的**残留缺口**；新增 P1-7（写账非事务）、P1-8（执行结果不回填）、P1-9（网络无硬超时）；新增 P2-14~16（风控截断无留痕 / 基准未知代码静默 / 日期参数无校验）。本轮为评审稿，未落地任何修改。
