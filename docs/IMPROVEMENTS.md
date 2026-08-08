# AI 自动交易体验机 · 代码评审与改进建议

| 项目 | 内容 |
|---|---|
| 版本 | 0.3（覆盖 v0.2 正文，保留历史落地记录） |
| 日期 | 2026-08-09 |
| 状态 | 评审稿（待确认优先级后落地） |
| 评审对象 | `ai_trader/` 全工程（引擎 / 风控 / 账本 / 回测 / 数据源 / 报表 / 部署） |

> 本文档基于当前代码逐行阅读 + 全量 72 个 pytest 用例通过（2026-08-09）验证。v0.1/v0.2 已落地项直接进入第 6 节"已验证"清单；已提未做项标注"存量，未落地"；本轮新增 P0-1、P0-2、P1-6 与 P2-9~13 等新发现。每条建议均指向具体文件与函数，便于落地与验收。

---

## 1. 总体评价

系统作为"AI 决策能力评测体验机"已达合格工程水平：

- ✅ 分层清晰、依赖可注入、核心逻辑可单测（72 个 pytest 用例全过）。
- ✅ 全量留痕：决策输入行情 + 原始输出均入库，任何一笔可回溯。
- ✅ 多引擎独立账本 + walk-forward 回测 + 幂等 / 交易日判断 / 单实例锁 / 日志留证 / 行情失败当日跳过等工程细节已完善（v0.1/v0.2 落地）。

距"无人值守、结果可当真"仍有三块短板：

1. **回测报表混入历史残留账户**（P0-1）——不同区间/引擎的陈旧曲线并列展示，误导对比；
2. **数据新鲜度无守卫**（P0-2）——日历降级或数据源滞后时，会用旧价制造"当日"假快照；
3. **成交模型 / 信号输入 / 政策归因仍停留在"可用但不够科学"**（P1-1~P1-6，多为存量）。

一句话大白话：**这台机器现在已经能规规矩矩每天自己跑、每笔决策都留底；但要"把结果当真"，还差两件事——回测报表别把上一轮的旧曲线混进来，行情没更新也别拿旧价格假装今天成交了。**

---

## 2. 🔴 P0（真 Bug / 数据可信度，建议最先处理）

### P0-1 回测报表混入历史残留账户（陈旧曲线并列展示，误导对比）

- **位置**：`run.py::run_backtest`（回测循环）／`aitrader/reporter.py::plot_backtest_curves`、`build_backtest_report`（遍历 `db.get_accounts()`）／`aitrader/database.py::get_accounts`
- **问题（现状 + 为什么是问题）**：`run_backtest` 只对"本轮选中的引擎"调 `Backtester.run()`（内部 `reset_account` 重放），**未选中的引擎账户保留上次回测的快照/成交**；而两个报表函数遍历 backtest.db 的**全部账户**。因此先跑过 `--backtest`（默认 both = rule + ai）后，再跑 `--backtest --engine rule`，图表/报告里仍会出现上一次的 ai（甚至 ai_policy）曲线，与当前 rule 曲线并列 → 把"不同区间、不同引擎"的陈旧数据当成同一次对比，直接误导评测结论。
- **建议**：① `run_backtest` 把本轮 `engine_type` 集合传给两个报表函数，只渲染本轮引擎的账户；② 或给回测库加 `run_id`（每次回测生成新 id 落库），报表按 `run_id` 过滤；③ 加单测：先跑双引擎再单引擎回测，断言报表只含当前引擎。
- **影响**：消除"看图说话"最大的误导源；改动集中在 `run_backtest` 与两个报表函数，风险低。

### P0-2 交易日历降级 + 无"数据新鲜度守卫"——工作日节假日 / 数据源滞后会用旧价成交

- **位置**：`aitrader/datasource.py::_load_trade_calendar`、`is_trading_day`／`aitrader/batch.py::run`、`_fetch_and_store`
- **问题**：① 交易日历进程启动时拉一次，失败置空 → `is_trading_day` 降级为"仅跳过周末"（SRS FR13 已知设计）；此时若当天是**工作日节假日**，会被当成交易日，akshare 返回**最近一个交易日的旧 K 线**（非空，不触发 v0.2 已修的行情失败保护）→ 照常决策并写入"节假日日期"的快照与成交。② 即便日历正常，**当日行情源未更新**（数据源滞后，或 15:30 时当日数据尚未发布）时，`bars[-1].close` 也是旧价，`prices` 直接取用 → 制造"当日"假快照。两者都是与 v0.2 已修"行情拉空"同级的**静默坏数据**入口。
- **建议**（改动小、推荐）：① `_fetch_and_store` 拉取后加"新鲜度守卫"：至少一个标的 `bars[-1].datetime.date() == 目标交易日` 才允许交易，否则当日跳过、写 `_warning=stale_bars`，与 v0.2 走同一路径；② 交易日历失败时在 `last_run.json`/日志显著告警（目前静默置空）；③ 单测：FakeDataSource 返回截止到 2 天前的旧 K 线 + 交易日，断言当日跳过。
- **影响（trade-off 说明）**：保守方案会**跳过**那些"当日数据尚未发布"的交易日（少成交几天但不产生假数据）；如不愿跳过，则至少应在快照里记录实际 `bar_date` 供审计。建议默认保守 + 记录 `bar_date`。
- **需验证**：新浪 `fund_etf_hist_sina` 当日数据的确切发布时间（15:30 时是否已有当日 K 线）。

---

## 3. 🟡 P1（可靠性 / 评测科学性）

### P1-1 回测/仿真"当日收盘价成交"偏乐观（存量 2.5，未落地，需拍板）

- **位置**：`aitrader/backtest.py::Backtester.run`／`aitrader/portfolio.py::execute_decisions`
- **问题**：15:30 收盘后决策却以**当日收盘价**成交 = 假设能在收盘价买入，实盘做不到（至少次日开盘），收益被高估。
- **建议**：① 严格做法：决策用截至当日行情、成交用**次日开盘价**（回测内错位一天，需拍板：回测变慢、需引入次日 open）；② 或保持现状，在 `backtest_report.html` / README 明确标注"收盘价成交 + 无滑点 = 乐观上界"。
- **影响**：评测结论更接近可实现收益。

### P1-2 行情未复权——ETF 分红除息让指标失真（存量 2.6，未落地，需调研接口）

- **位置**：`aitrader/datasource.py::AkShareDataSource.fetch_daily_bars`
- **问题**：`fund_etf_hist_sina` 返回**未复权价**；分红除息价格跳空 → 双均线假金叉/假死叉、AI 看到不连续序列，长期失真累积。
- **建议**：调研新浪/东财**前复权**参数并切换（**需验证接口能力**）；短期无法接入则必须在 README / 回测报告注明"未复权、忽略分红除息"。
- **影响**：评测数据可信度的根基。

### P1-3 喂给模型的信号太弱（存量 3.1，未落地）

- **位置**：`aitrader/engines/deepseek.py::_build_prompt`
- **问题**：提示词只有 20 个收盘价 + 现金 + 持仓，模型要心算均线/涨跌幅/波动率——考验算术而非决策能力。
- **建议**：算好 MA5/10/20、20 日累计涨跌幅、RSI、20 日波动率、距 20 日高/低点位置后注入。
- **影响**：AI 决策质量可复现，评测更公平。

### P1-4 政策版无法统计归因（存量 3.2，未落地）

- **位置**：`aitrader/batch.py::_fetch_policy`（政策文本未落库）／`aitrader/engines/deepseek.py::include_policy`／`aitrader/reporter.py`
- **问题**：只对比曲线，"政策有没有用"无统计依据；政策文本不存档 → 政策版无法做历史回测。
- **建议**：① 新增 `policy_archive` 表每日**原样存档**（含日期），从启用日攒历史；② 攒够后用"历史当日可见政策"喂入做历史回测；③ 输出两版 AI **逐日决策差异表** + 分歧事后归因；④ 报表加 `decisions.validation` 的"乱写率"视图。
- **影响**：三引擎对比从"看图说话"变"可统计"。

### P1-5 无止损 / 仓位进阶（存量 3.4，未落地）

- **位置**：`aitrader/risk.py::validate_buy`／`aitrader/portfolio.py::execute_decisions`
- **问题**：只有单笔 ≤30% / 单日 ≤50%，无止损；规则与 AI 都是趋势跟踪，单边下跌一直扛。
- **建议**（风险从小到大）：① 单标的浮亏硬止损（每日刷新后检查自动卖出）；② 总资产自峰值回撤熔断；③ 盈利后移动止损。
- **影响**：回撤可控，评测更接近真实风控。

### P1-6 AI 回测首跑调用成本无上限（新发现）

- **位置**：`run.py::run_backtest`／`aitrader/backtest.py::Backtester.run`
- **问题**：无缓存首次回测**逐日调用 DeepSeek**：4.5 年 ≈ 1100 交易日 → ai 引擎约 1100 次 API 调用（显式跑 ai_policy 再翻倍），成本/耗时不可控；缓存只在"完全相同区间 + 相同持仓状态"时命中。
- **建议**（需拍板 trade-off）：① 回测前打印预计调用次数与提示；② 提供 `--max-calls` 上限或"每 N 日决策一次"的抽样模式；③ 缓存落 SQLite，支持崩溃后断点续跑。
- **影响**：避免一次误操作烧掉大量 API 额度。

---

## 4. 🟢 P2（工程体验 / 可运维性）

| # | 类别 | 位置 | 问题（现状） | 建议 |
|---|---|---|---|---|
| P2-1 | 存量 4.1 | `aitrader/datasource.py`／`aitrader/backtest.py`／`aitrader/batch.py::_fetch_and_store` | akshare 单点，挂了就全挂；`bars` 表**只写不读**（`get_bars` 无任何调用点，已确认），批处理与回测每次都联网拉全量 | `fetch_daily_bars` 先查 `bars` 表、缺的再补拉；数据源失败时 fallback 其他源 |
| P2-2 | 存量 4.2 | `aitrader/reporter.py::plot_compare` | 日报对比图仍是绝对"总资产（1e6 轴）" | 与回测图统一为净值起点=1；加逐笔成交明细 + "乱写率"视图 |
| P2-3 | 存量 4.3 | `aitrader/reporter.py`（两处 rcParams） | `Microsoft YaHei` 硬编码，跨平台乱码 | 走 `matplotlib.font_manager` 探测可用字体 |
| P2-4 | 存量 4.4 | `aitrader/database.py::_connect` | 无 WAL、`trades/decisions` 无索引（按 account_id 查）、`save_bars` 逐条 INSERT、每操作重开连接 | `PRAGMA journal_mode=WAL`、加索引、`executemany`、改长连接 |
| P2-5 | 存量 4.5 | `config.json`／`aitrader/config.py::Settings` | `max_buy_count` 等有默认值但 config.json 未暴露 | 止损/熔断/成交时滞/重试等新参数全部进 config |
| P2-6 | 存量 4.6 | `run.py::_run` | 失败只在 `last_run.json`/日志 | 失败/连续 N 天无成交时主动通知（企业微信 / Server酱 / 邮件） |
| P2-7 | 存量 4.7 | `run.py` | `--db` 批处理覆盖每日账本库、回测覆盖回测库，语义混用易误覆盖 | 回测改 `--bt-db`，加"确认覆盖"保护 |
| P2-8 | 存量 4.8 | `.env`／`aitrader/config.py::load_settings` | key 为空/占位/异常无启动校验 | 启动时校验并给出明确提示 |
| P2-9 | 新发现 | `run.py::run_backtest` | `--engine ai` 无 key 时**静默改跑 rule**（有提示但行为与请求不符，易误读结果） | 请求引擎不可用直接报错退出，或明确标注"已降级为 rule"并体现在结果标签 |
| P2-10 | 存量 3.6 | `run.py::run_backtest`（`AI_CACHE_PATH` 整体读写） | 缓存全量 JSON 读写、非原子（进程崩溃损坏/丢失） | 原子替换（temp + `os.replace`）或落 SQLite |
| P2-11 | 新发现 | `aitrader/engines/deepseek.py::_call` | `resp.json()` 抛 `JSONDecodeError`（ValueError）时未单独捕获，原始 body 未留痕（v0.2 只处理 KeyError/IndexError/TypeError） | 捕获 ValueError，把响应文本拼进异常供降级留痕 |
| P2-12 | 新发现 | `aitrader/backtest.py::compute_benchmark` | 基准无手续费，与带成本策略对比略偏袒基准 | 需拍板：基准按佣金折算，或报告标注"基准未计成本" |
| P2-13 | 新发现 | `aitrader/batch.py::_fetch_policy` | 只跑 rule 引擎也联网拉政策（浪费 + 失败时 exception 噪音） | 引擎集不含 include_policy 引擎时跳过拉取 |

---

## 5. 落地优先级建议

**第一梯队（改动小、直接堵数据可信度口子，建议立即做）**
1. 回测报表只渲染本轮引擎账户 + 单测（P0-1，约半小时）
2. 行情"数据新鲜度守卫" + 日历失败告警（P0-2）
3. `resp.json()` 异常留痕（P2-11，5 分钟）

**第二梯队（评测科学性，涉及拍板 / 调研）**
4. 成交价假设拍板：次日开盘价 or 标注乐观上界（P1-1）
5. 复权数据调研与接入（P1-2）
6. 提示词注入技术特征（P1-3）
7. 政策存档入库 + 决策差异归因（P1-4）

**第三梯队（成本控制 / 功能扩展 / 工程打磨）**
8. AI 回测成本上限 / 调用提示（P1-6）
9. 止损 / 仓位进阶（P1-5）
10. 存量 P2-1~P2-8 逐项打磨

---

## 6. 验收清单 + 落地记录

### 已验证（v0.1/v0.2 已落地，2026-08-09 代码复核 + 72 用例全过确认）

- [x] 日志落盘 `data/logs/app.log` 5MB×5 轮转（`run.py::setup_logging`）
- [x] `data/last_run.json` 运行留证（`run.py::write_last_run`）
- [x] 单实例文件锁（`aitrader/lock.py::FileLock`）
- [x] AI 语义校验写入 `decisions.validation` + 旧库迁移（`deepseek._validate` / `database._init_schema`）
- [x] `EngineType` 含 `ai_policy`（`models.py`）
- [x] `--engine ai_policy` 每日批处理正确过滤（`run.py::select_daily_engines`，test_v02 覆盖）
- [x] 行情拉取失败当日跳过交易 + `last_run` 告警（`batch.run` 返回 `_warning`，test_v02 覆盖）
- [x] DeepSeek 单条解析容错 + `amount` 字符串兼容（`deepseek._parse` / `_to_float`）
- [x] DeepSeek / akshare 网络重试（`util.retry_call`，接入行情/政策/AI 三处）
- [x] 回测 O(log N) 日期索引（`backtest.py` bisect）
- [x] 回放无前视（`fetch_daily_bars` 加 `end_date`）／ 交易日判断 ／ 同日幂等
- [x] walk-forward 回测 + 指标 + 基准（`backtest.py`）
- [x] AI 响应缓存（`deepseek._call`，按 prompt 哈希，ai/ai_policy 共享）

### 验收清单（v0.3 待落地，落地后逐项打勾）

- [x] 回测报表只含本轮引擎账户（`engine_types` 过滤，先双引擎再单引擎断言无陈旧曲线）—— P0-1
- [x] 行情非"当日最新"（严重陈旧 >5 自然日）时当日跳过交易并告警—— P0-2
- [x] 交易日历拉取失败在日志 / 非交易日分支显著告警（`calendar_ok`）—— P0-2
- [x] `resp.json()` 异常时原始响应文本拼进异常留痕—— P2-11
- [ ] 回测报告 / README 标注成交价假设（收盘价 / 次日开盘价）—— P1-1
- [ ] 提示词含技术特征字段，回测可复现—— P1-3
- [ ] `policy_archive` 表从启用日起存档政策文本—— P1-4
- [x] AI 回测前提示预计 API 调用次数（防误操作烧额度；成本上限未做）—— P1-6
- [x] `--engine ai/ai_policy` 无 key 时回测直接报错退出—— P2-9

> 落地记录（2026-08-09，v0.3）：完成 P0-1（reporter 按 `engine_types` 过滤本轮引擎）、P0-2（`stale_bars` 新鲜度守卫 + 日历失败告警）、P2-9（无 key 回测 AI 报错）、P2-10（AI 缓存原子写 `os.replace`）、P2-11（`resp.json()` 非 JSON 留痕）、P2-13（无政策引擎跳过拉政策）、P1-6（回测前预计调用次数提示）；新增 `tests/test_v03.py`（5 用例），全量 77 用例通过；冒烟验证报表只含本轮引擎。

### 落地记录

- **v0.1（2026-08-09，已落地）**：日志落盘 + `last_run.json`、单实例锁 `aitrader/lock.py`、AI 语义校验写入 `decisions.validation`、`EngineType` 补 `ai_policy`；新增 `tests/test_reliability.py`（9 用例）。
- **v0.2（2026-08-09，已落地）**：`ai_policy` 批处理过滤、行情失败当日跳过、DeepSeek 解析容错、网络重试、回测 O(log N)；新增 `tests/test_v02.py`（12 用例），全量 72 用例通过。
- **v0.3（本轮评审，2026-08-09）**：新增 P0-1（回测报表混入历史残留账户）、P0-2（日历降级 + 无数据新鲜度守卫）、P1-6（AI 回测成本无上限）、P2-9~13（引擎降级语义 / 缓存原子写 / JSONDecode 留痕 / 基准成本 / 政策拉取时机）；其余为 v0.1/v0.2 存量的未落地短板。
