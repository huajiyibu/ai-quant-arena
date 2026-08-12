# AI Quant Arena · 代码评审与改进建议

| 项目 | 内容 |
|---|---|
| 版本 | 0.4（覆盖 v0.3 正文，反映代码 v0.16 现状） |
| 日期 | 2026-08-12 |
| 状态 | 评审稿（待确认优先级后落地） |
| 评审对象 | `ai_trader/` 全工程（引擎 / 风控 / 账本 / 回测 / 数据源 / 报表 / 部署 / 通知） |

> 本文档基于当前代码逐行阅读 + 全量 175 个 pytest 用例通过（2026-08-12）验证。**双轨分工**：工程可靠性轨（本文件）只管"机器能否安全、幂等、可观测地自动跑"；预测/决策质量轨（`docs/PREDICTION_IMPROVEMENTS.md`）管"决策质量与评测闭环"（PP-1~PP-8、N-1~N-12、A/B 实验），二者不重复。v0.1~v0.16 已落地项收敛进第 6 节"落地记录"；本轮新发现以 🔴 标注。

---

## 1. 总体评价

系统作为"自动量化体验机"已达**较高的工程成熟度**：

- ✅ 分层清晰、核心逻辑纯函数可单测（175 用例全过）。
- ✅ 全量留痕（prompt / raw / validation / execution_result）、幂等（快照 + `batch_runs` 标记）、交易日判断、单实例锁、日志轮转、行情失败当日跳过、数据时点硬校验（bars[-1]==决策日）、政策 15:30 截断防前视、双 bars（真实盘特征复权）、网络硬超时 + IPv6 修复等已完善（v0.1~v0.16 落地）。
- ✅ 真实盘已接入 A/B 最优配置（feature_inject + market_env + hfq 复权），现金生息、止损/止盈/滑点/置信度门槛/历史盈亏反馈等实验参数齐备。

距"可无人值守、可自我改进的自动量化 Agent"仍差五块：

1. **`--force` / 崩溃重跑非幂等**（P0-1）——同日重跑可能重复成交 + 重复计息，账本被污染；
2. **可观测性有洞**（P0-2/P0-3）——告警只看第一个引擎、日报"数据截至"失效；
3. **N-9 补跑没接线**（P0-4）——部署脚本仍跑裸 `run.py`，连续关机缺口照旧；
4. **回测评测口径**（P0-5/P1-1）——Rank IC 混入未成交 buy、归因不含佣金；
5. **Agent 化缺"实验台账 + 自检"**（P1-5/P2-8）——回测不落库、无 `--health`。

一句话大白话：**机器现在每天能自己跑、每笔都留底；但要当"自动 Agent"放心用，还差两件事——同一件事别重复算两次（幂等），坏了/卡了/空转要能让我一眼看到（可观测）。**

---

## 2. 🔴 P0（真 Bug / 数据可信度，建议最先处理）

### P0-1 `--force` / 崩溃重跑同日非幂等：重复成交 + 重复计息

- **位置**：`aitrader/batch.py::_run_engine`／`aitrader/portfolio.py::apply_cash_interest`／`aitrader/database.py::add_trade`
- **问题（现状 + 为什么是问题）**：
  - `trades` 表无 `UNIQUE(account_id, date, symbol, action)` 唯一约束：`--force` 同日重跑时，AI 因温度/状态变化可能给出新决策，直接再 INSERT 一笔新成交（重复建仓/平仓），`trade_count`、`compute_metrics`、归因全部被重复计入；
  - `apply_cash_interest` 无"当日已计息"标记：`--force` 重跑基于**已含当日利息**的现金再计一次息（崩溃窗口同理：`save_state` 已写、快照未写时重跑 → 双计息）；
  - `update_decision_execution` 的 `WHERE execution_result IS NULL` 让重跑的成果无法回填。
- **建议**：① 重新定义 `--force` 语义——要么"先清当日该账户 trades/decisions/batch_run 再重跑"，要么"只重估值不重成交"；② `trades` 加唯一约束 + upsert（同日同标的同向视为更新）；③ 利息幂等：`account_states` 记 `last_interest_date`，同日起跳过，或改为"按快照日差计息"。
- **影响**：无人值守下 `--force` 是排障常用手段，当前会把账本改坏——本轮最需先修。

### P0-2 告警只看第一个引擎账户——AI 引擎空转/回撤不告警

- **位置**：`run.py::_maybe_notify`
- **问题**：`for et in engines: a = db.get_account_by_engine(et); if a: acc = a; break` —— 只取**第一个**找到的账户（默认 rule 引擎）就 `break`。`check_alerts` 的"连续无成交 / 净值回撤"只用该账户判定 → AI 引擎长期空转或深回撤时**不触发任何告警**，N-10 实际只对 rule 生效。
- **建议**：跨全部引擎账户聚合——回撤取各引擎中最大者、成交数取全部账户交易日并集；或对每个引擎分别判定。
- **影响**：通知功能实际覆盖与文档不符；改动小。

### P0-3 日报头部"数据截至"失效（逻辑写反）

- **位置**：`aitrader/reporter.py::build_daily_report`
- **问题**：`if bar_date > today: data_until = max(...)` 只在"行情日期晚于今天"（正常不可能发生）时更新头部 `数据截至`；真正需要暴露的**陈旧**（`bar_date < today`，如实时补全失败回退旧价）在头部永远显示"—"，只有卡片小字 `（估值截至 X）`。B-2/F-11 本意是"显著暴露数据滞后"，实际没暴露。
- **建议**：头部 `数据截至 = max(所有账户 bar_date)`，且 `bar_date < today` 时高亮红色警示；卡片与头部口径统一。
- **影响**：用户靠日报判断"今天的数据新不新"，当前会误以为数据是新的。

### P0-4 N-9 `--catch-up` 未接线到部署脚本——连续关机缺口照旧

- **位置**：`scripts/install_task.ps1`（定时任务 + 启动项都跑裸 `run.py`）
- **问题**：N-9 已实现 `--catch-up`，但部署脚本仍 `pythonw run.py`（不带参数）。周末 + 节假日 + 周一早上开机，登录启动项只补"今天"，前几个工作日缺口依旧，资金曲线 / 基准对照 / Rank IC 样本继续断档。
- **建议**：启动项 `.lnk` 的 `Arguments` 改为 `run.py --catch-up`；定时任务保持 15:30 裸跑（当天由它处理）。
- **影响**：一行改动让"补跑"真正生效；否则 N-9 是空转。

### P0-5 回测 Rank IC 混入"未成交"的 buy——校准被污染

- **位置**：`aitrader/backtest.py::Backtester.run`（`buys` 收集在 `execute_decisions` **之前**）
- **问题**：`buys` 收集所有 `action=="buy" and dec.valid` 的决策，但其中一部分随后被 `execute_decisions` 拒绝（已持仓 / 现金不足 / 单日超限）。Rank IC 把"决策意图"与"实际成交"混在一起，评估的是"模型想买什么"而非"模型实际赚了什么"。
- **建议**：只收集**当日实际成交的 buy**（从当日 `trades` 或 `execution_results` 过滤 `executed:buy` 的决策）再进 `compute_forward_returns`；batch 的 `_calibrate_forward_returns` 同样应只回填已成交 buy（当前 `get_uncalibrated_buys` 含被拒 buy）。
- **影响**：评测科学性；改动小（过滤条件一行）。

---

## 3. 🟡 P1（可靠性 / 评测科学性）

### P1-1 归因 / 浮盈不含佣金——收益口径偏乐观
- **位置**：`aitrader/attribution.py::attribute_trades`（`pnl=(卖价-买价)×量`）／`aitrader/models.py::Position.unrealized_pnl`
- **问题**：归因复盘与持仓浮盈都不扣买卖佣金（佣金只体现在 `AccountState.cash` 层面）。高换手时"标签盈亏"系统性偏高。
- **建议**：归因按 `(卖价×(1-佣金) - 买价×(1+佣金))×量` 净口径；`Position` 加含佣 `cost_basis` 算浮盈。
- **影响**：让"哪类理由赚钱"的复盘更诚实。

### P1-2 AI 对"已持仓标的"再下 buy，引擎校验层不拦（validation 记 ok）
- **位置**：`aitrader/engines/deepseek.py::_validate`
- **问题**：`_validate` 对 buy 只查 amount/confidence/buy_count，不查 `已持仓`；`execute_decisions` 才以 `risk_rejected:已持仓` 拒绝 → "决策质量"统计里这类错误 buy 被记 `validation=ok`，乱写率失真。
- **建议**：`_validate` 增加 `d.symbol in ctx.account.positions → validation="already_holding"`（与 execute 一致）。
- **影响**：决策留痕更精确；改动约 3 行。

### P1-3 实时行情接口无重试——偶发抖动即当日剔除该标的
- **位置**：`aitrader/datasource.py::_fetch_realtime`（直接单次 `requests.get`）
- **问题**：新浪历史接口滞后 1 交易日，`_fetch_realtime` 是当日 bar 的唯一来源；它不经过 `retry_call`，一次超时/抖动 → 该标的被判陈旧剔除，可能整日跳过交易。
- **建议**：`_fetch_realtime` 接入 `retry_call`（1s/2s），与 `fetch_daily_bars` 一致。
- **影响**：提升无人值守的成交连续性；改动约 2 行。

### P1-4 交易日历拉取无重试 / 无本地缓存
- **位置**：`aitrader/datasource.py::_load_trade_calendar`
- **问题**：进程启动拉一次，失败置 `calendar_ok=False` → 当日（若为工作日）整日保守跳过。无人值守下"偶发一次网络失败 = 少跑一天"。
- **建议**：`retry_call` 包装 + 成功落盘 `data/trade_calendar.json` 复用（次日启动先读缓存，失败才联网）。
- **影响**：减少因日历抖动导致的漏跑。

### P1-5 回测结果无结构化留档（run ledger）——"自动 Agent 自我改进"的地基
- **位置**：`run.py::run_backtest`／`aitrader/backtest.py`
- **问题**：每次回测只打印 + 生成 HTML，配置、指标、基准、Rank IC、API 调用量都不落库。跑过的实验无法历史对比，"哪次改了 system / 参数得出哪个结果"全靠记忆，多 AI 评审协作时容易对不上账；`record_decisions` 落库的决策也**没有回填 execution_result**（回测里 `_` 丢弃）。
- **建议**：新增 `backtest_runs` 表（run_id、时间、区间、config 快照 JSON、各引擎 metrics、bench、rank_ic、api_calls/cache_hits、fill/adjust/feature 等实验参数），每次回测写入；`--list-runs` 可查；回测落库决策顺带回填 execution_result。
- **影响**：把"评测"从一次性动作变成可追踪记录，是后续自动调参/自动对比的必需品。

### P1-6 `write_last_run` 错误路径清空上次正常记录
- **位置**：`run.py::write_last_run`（`main` 的 except 分支）
- **问题**：运行中途异常时写 `mode=error` 且 `engine_results={}`，覆盖掉"上次成功"的信息；排障时无法对比"上次正常 vs 这次失败"。
- **建议**：异常路径保留上次的 `engine_results`（或追加 `last_ok` 字段）。
- **影响**：可观测性；改动小。

### P1-7 idle 告警用日历天数近似交易日 + 长期空仓误报
- **位置**：`run.py::_maybe_notify`（`cutoff = date - timedelta(days=n*2)`）／`aitrader/notify.py::check_alerts`
- **问题**：① 用 `n*2` 天近似 N 个交易日不精确（节假日/长假偏差大）；② `trades_count==0` 无条件告警，对"策略本来就长期空仓"（AI 保守风格）是持续误报噪音。
- **建议**：用 `is_trading_day` 过滤统计最近 N 个交易日成交；空转告警区分"有持仓但无成交"（真异常）与"本来就空仓"（正常，降级为提示）。
- **影响**：告警可信度。

---

## 4. 🟢 P2（工程体验 / 可运维性）

| # | 类别 | 位置 | 问题（现状） | 建议 |
|---|---|---|---|---|
| P2-1 | 存量 P2-5 | `config.json`／`aitrader/config.py::Settings` | `feedback_n`/`slippage_bps`/`stop_loss_pct`/`take_profit_pct`/`min_confidence_buy`/`temperature`/`system_prompt_extra`/`max_buy_count` 有默认值但 config.json 未暴露，改参数只能靠 CLI | 新参数全部进 config.json（并入 risk 块），CLI 覆盖时打印"已覆盖默认" |
| P2-2 | 存量 P2-7 | `run.py` | `--db` 在批处理/回测语义混用，误传会污染每日账本 | 回测改 `--bt-db` 独立参数 + "确认覆盖"保护 |
| P2-3 | 存量 P2-3 | `aitrader/reporter.py`（两处 rcParams） | `Microsoft YaHei` 硬编码，跨平台乱码 | `matplotlib.font_manager` 探测可用中文字体 |
| P2-4 | 存量 P2-4 | `aitrader/database.py::_connect` | 无 WAL、`trades/decisions` 无 account_id 索引、每操作重开连接 | `PRAGMA journal_mode=WAL` + 索引 + 长连接 |
| P2-5 | 存量 P2-2 | `aitrader/reporter.py::plot_compare` | 日报资金曲线仍是绝对"总资产（1e6 轴）"，与回测净值图口径不一 | 统一净值起点=1（与 `plot_backtest_curves` 一致） |
| P2-6 | 存量 P2-8 | `.env`／`load_settings` | key 为空/占位无启动校验 | 启动校验并给出明确提示（回测已做，批处理未做） |
| P2-7 | 新 | `run.py` | 回测参数覆盖 config 时无冲突告警（如 `--fill close` 但 config `next_open`） | 覆盖时打印"用 X 覆盖 config 的 Y" |
| P2-8 | 新 | `run.py`／`notify.py` | 无 `--health` 自检命令；"连续 N 天 last_run 陈旧/未跑"不在告警条件里 | 加 `--health`（网络/日历/key/config 一致性/bars 新鲜度/last_run 新鲜度）；`check_alerts` 增加 `last_run_stale_days` |
| P2-9 | 新 | `scripts/` | 脚本（bench_metrics/check_daily/corr_analysis/stop_grid）无统一入口文档、无测试 | 给脚本加短注释头（用法/依赖/是否烧 API），纳入 README 工具清单 |

---

## 5. 落地优先级建议

**第一梯队（改动小、直接堵数据可信度/可观测口子，建议立即做）**
1. P0-1 `--force` 幂等重定义（清当日留痕再跑 / 唯一约束 + 利息幂等）——约 1~2 小时
2. P0-2 告警跨引擎聚合（5 分钟）
3. P0-3 日报头部"数据截至"修正（5 分钟）
4. P0-4 `install_task.ps1` 启动项接 `--catch-up`（1 行）
5. P0-5 Rank IC 只统计实际成交 buy（10 分钟）

**第二梯队（评测科学性 / 无人值守稳健性）**
6. P1-5 回测 run ledger 落库 + `--list-runs`
7. P1-1 归因/浮盈净佣金口径
8. P1-3/P1-4 实时行情与交易日历重试
9. P1-2 `_validate` 补 already_holding
10. P1-6/P1-7 告警与 last_run 改进

**第三梯队（工程打磨）**
11. P2-1~P2-9 逐项（config 暴露 / `--bt-db` / 字体 / WAL / 净值轴 / key 校验 / `--health` / 脚本文档）

> 预测质量轨剩余项（PP-7 政策归档回测、PP-8 标的池扩池）在 `docs/PREDICTION_IMPROVEMENTS.md`，需烧 API / 攒数据，不在本文件重复。

---

## 6. 验收清单 + 落地记录

### 已验证（v0.1~v0.16 已落地，2026-08-12 代码复核 + 175 用例全过）

- [x] 日志落盘 `data/logs/app.log` 5MB×5 轮转／`last_run.json` 运行留证／单实例文件锁（`lock.py`）
- [x] 同日幂等（快照 + `batch_runs` 标记，N-2 只认 done）／交易日判断／`--date` 回放无前视／`source`(real/replay) 隔离
- [x] 行情失败当日跳过 + 数据时点硬校验（bars[-1]==决策日，v0.9）／实时行情补全（v0.8）／政策 15:30 截断防前视（v0.9）
- [x] DeepSeek 单条解析容错／语义校验留痕（validation）／网络重试（retry_call）／响应缓存原子写 + 缓存键含 model/temperature/system/prompt
- [x] 回测 O(log N)、报表只渲染本轮引擎、基准佣金同口径、`fill_mode:next_open` + `adjust:hfq` 默认
- [x] 双 bars（真实盘特征复权，N-1）／止损/止盈/滑点/置信度门槛/历史盈亏反馈（PP-5/PP-6/P2-2/PP-4）／现金生息（v0.16）
- [x] 真实盘 Rank IC 回填 + 日报（分桶胜率/归因复盘/基准对照/数据截至/今日决策）／引擎级异常隔离（A-2）／收盘守卫（A-3）／未来日期拒绝（A-4）／回测 end 默认最近已收盘（A-5）
- [x] `--catch-up` 补跑（N-9，**⚠️ 未接线到部署脚本，见 P0-4**）／通知（N-10，**⚠️ 只监控第一引擎，见 P0-2**）／bars 缓存兜底（N-11）／API 调用量记账（N-8）

### 验收清单（本轮待落地，落地后逐项打勾）

- [ ] `--force` / 崩溃重跑不再重复成交与重复计息（同日清留痕重跑，或 唯一约束 + 利息幂等）—— P0-1
- [ ] 告警跨全部引擎账户聚合判定—— P0-2
- [ ] 日报头部"数据截至"显示实际 bar_date 且陈旧高亮—— P0-3
- [ ] 启动项接 `--catch-up`—— P0-4
- [ ] Rank IC / forward_return 只统计实际成交 buy—— P0-5
- [ ] 回测 run ledger 落库 + `--list-runs`—— P1-5
- [ ] 归因/浮盈净佣金口径—— P1-1
- [ ] `_validate` 补 already_holding—— P1-2
- [ ] 实时行情 + 交易日历重试—— P1-3/P1-4
- [ ] `--health` 自检 + last_run 陈旧告警—— P2-8

### 落地记录（v0.1~v0.16 里程碑，详见仓库 git log / 各版本评审）

- **v0.1~v0.2（08-09）**：日志/last_run/单实例锁/语义校验留痕；ai_policy 批处理过滤、行情失败跳过、DeepSeek 解析容错、网络重试、回测 O(log N)。
- **v0.3~v0.4（08-09）**：回测报表只渲染本轮引擎、数据新鲜度守卫 + 日历告警、逐标的陈旧剔除 + bar_date、`batch_runs` 防崩溃重复成交、execution_result 回填、日期参数校验。
- **v0.5~v0.7（08-09）**：成交假设校准（PP-1）、system 三段式 + 缓存键修正（PP-3）、特征注入（PP-2）、置信度门槛 + Rank IC 闭环（PP-4）、复权接入回测、网络硬超时 + IPv6 修复（P1-9）。
- **v0.8~v0.9（08-10）**：实时行情补全（治新浪滞后 1 日）、政策 15:30 截断防前视、行情"当日硬校验"。
- **v0.10~v0.12（08-10）**：真实盘接入最优配置（A-1）、未来日期拒绝（A-4）、引擎异常隔离（A-2）、收盘守卫（A-3）、回测 end 默认最近已收盘（A-5）、配置对齐（P2-1）、滑点（P2-2）、真实盘 Rank IC 回填（B-1）、日报升级（B-2）、`batch_runs` 只认 done（N-2）、`source` 隔离（N-12）、信心分桶（N-4）。
- **v0.13~v0.14（08-11）**：双 bars 真实盘特征复权（N-1）、成交口径说明（N-3）、归因标签 + 复盘（N-5）、`--market-env` 接线（N-6）、API 记账（N-8）、`--catch-up`（N-9）、通知（N-10）、bars 缓存兜底（N-11）。
- **v0.15（08-11）**：烧 API A/B——市场环境注入开启（夏普 0.98→1.57）、历史盈亏反馈可选、止损/止盈网格无有效组合保留默认关、标的池相关性 0.75~0.87 高度冗余（完整结论在 PREDICTION 文档）。
- **v0.16（08-12）**：现金生息（货基假设，空仓也被正确计价）。
- **v0.17（08-12，落地本轮评审 P0 + 轻量 P1，测试 181 全过）**：**P0-1** 计息幂等（`account_states.last_interest_date`，`--force`/崩溃重跑不再双计息）；**P0-2** 告警跨全部引擎账户独立判定（AI 空转/回撤也能触发）；**P0-3** 日报"数据截至"修正（取所有账户 bar_date 最大值，陈旧时头部高亮 ⚠️）；**P0-4** 部署启动项接入 `run.py --catch-up`（连续关机缺口真正可补）；**P0-5** 回测 Rank IC 只收实际成交 buy、`get_uncalibrated_buys` 排除被拒 buy；**P1-1** 归因/已平仓配对改为净口径（扣双边佣金）；**P1-2** 已持仓再下 buy 引擎校验标 `already_holding`；**P1-3** 实时行情接口接入 `retry_call` 重试；**P1-6** 失败时 `last_run.json` 保留上次成功的 engine_results。测试 `test_v17.py` 6 条 + 更新旧测试净口径断言。
