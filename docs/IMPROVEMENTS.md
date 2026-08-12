# AI Quant Arena · 代码评审与改进建议（自动量化 Agent 视角）

| 项目 | 内容 |
|---|---|
| 版本 | 0.6（覆盖 v0.5 正文，反映代码 v0.18 现状） |
| 日期 | 2026-08-12 |
| 状态 | 评审稿（待确认优先级后落地） |
| 评审对象 | `ai_trader/` 全工程（引擎 / 风控 / 账本 / 回测 / 数据源 / 报表 / 部署 / 通知） |

> 本文档基于**真实磁盘代码（v0.18，git HEAD=91bc8a6，184 个 pytest 用例通过，2026-08-12 主 agent 亲自用终端逐行核实）**验证，非 read_file 缓存视图。
> **双轨分工**：工程可靠性轨（本文件）管"机器能否安全、幂等、可观测地自动跑"；预测/决策质量轨（`docs/PREDICTION_IMPROVEMENTS.md`）管"决策质量与评测闭环"（PP-1~PP-8、N-1~N-12、A/B 实验），二者不重复。v0.1~v0.18 已落地项收敛进第 7 节"落地记录"；本轮新发现以 🔴 标注。

---

## 1. 总体评价

系统作为"自动量化体验机"已达**很高的工程成熟度**：v0.13~v0.18 又堵掉了上一轮评审（v0.4/v0.5）的几乎所有 P0/P1 口子（双 bars 复权、--catch-up、通知、API 记账、口径说明、engine 隔离、收盘守卫、未来日期拒绝、cash 生息、日报数据截至、IC 只收成交、净佣金、already_holding、实时重试、last_run 保留上次正常）。

**本轮（v0.6）主 agent 亲自读真实磁盘代码**：v0.5 列出的 P0/P1/P2 项**全部核实属实**（trades 无唯一约束、config risk 块仅 3 字段、无 backtest_runs、无 --health、单一数据源、日历无缓存、政策不归档、成本无累计、idle 用 n*2 天近似、plot_compare 仍是 1e6 绝对轴），同时**新增发现 1 个 v0.5 未提及的真实 P0 bug + 若干 P2 级小点**。

距"可无人值守、可自我改进的**自动量化 Agent**"仍差五块：

1. **`--force` 同日重跑仍可重复成交**（P0-1）——`trades` 无唯一约束，排障常用手段会把账本改坏；
2. **🔴 回撤告警永不触发**（P0-3，本轮新发现）——`_maybe_notify` 给 `check_alerts` 传单元素快照，`max_drawdown_alert` 形同虚设；
3. **回测实验无台账**（P1-1）——每次回测的配置/指标/成本不落库，"哪次改了什么得出什么结果"全靠记忆，**自我改进循环的地基缺失**；
4. **真实盘与回测存在口径/参数漂移**（P0-2）——成交假设不同（真实盘收盘 / 回测 next_open），`config.json` 的 risk 块未暴露滑点/止损/置信度门槛，真实盘恒为 0；
5. **无人值守缺自检与兜底**（P1-2/P1-3/P1-4）——无 `--health`、单一数据源、交易日历无本地缓存。

一句话大白话：**机器现在每天能自己跑、每笔都留底；但要当"自动 Agent"放心用，还差三件事——`--force` 别再把手头这天的账重复记一遍（幂等）、告警要真的能响（回撤阈值修好）、每次回测/每天运行都留下可对比、可追溯的台账和成本（自我改进的燃料）。**

---

## 2. 🔴 P0（真 Bug / 数据可信度 / 无人值守风险，建议最先处理）

### P0-1 `--force` 同日重跑仍可能重复成交——`trades` 无唯一约束

- **位置**：`aitrader/batch.py::_run_engine`／`aitrader/database.py::add_trade`（真实 DDL 已核实，`trades` 无 `UNIQUE(account_id, date, symbol, action)`）
- **现状**：v0.17 已用 `last_interest_date`（计息幂等）+ `batch_runs`（崩溃重跑只认 done）堵住两个口子；但 `--force` 时 `_run_engine` **跳过** `has_snapshot / has_batch_run` 检查直接执行，AI 因温度/状态/特征变化可能给出**不同的当日决策** → `add_trade` 再 INSERT 一笔新成交（同日同账户同标的同向），`trade_count`、`compute_metrics`、归因全部被重复计入。
- **建议**：① 重新定义 `--force` 语义——"先清当日该账户 `trades`/`decisions`/`batch_runs` 再重跑"（排障场景），而非"叠加"；② 无论如何给 `trades` 加唯一约束 + upsert（同日同标的同向视为更新，保留首次 reason）；③ `--force` 打印"已清除当日 N 条旧留痕"。
- **影响**：无人值守下 `--force` 是排障常用手段，当前仍可能污染账本——本轮最需先修。

### P0-2 真实盘与回测成交/风控口径漂移——改进点"看得见但难对齐"

- **位置**：`run.py`（回测 CLI 参数）／`config.json`（risk 块，真实文件已核实仅 `max_position_pct/max_daily_buy_pct/commission_rate` 三字段）／`aitrader/portfolio.py::execute_decisions`
- **现状**：
  - 真实盘以**当日收盘价**成交（`last_run.json` 的 `fill_note` 已写明），回测默认 `fill_mode:next_open`（真实 `run_backtest` 支持 `--fill` 覆盖）——两者口径本就不同，回测 +26% 不代表真实盘；
  - `config.json` 的 `risk` 块**只暴露** 3 字段，`slippage_bps / stop_loss_pct / take_profit_pct / min_confidence_buy` 用 pydantic 默认值（0/0/0/0）→ **真实盘恒无滑点、无止损止盈、无置信度门槛**；而回测可 `--slippage/--stop-loss/--min-confidence` 临时开启 → 两套环境参数可漂移。
- **建议**：① `config.json` 的 risk 块补全新参数（并入 risk 块，见 P1-8）；② 新增 `--print-config`/`--check-config` 打印"真实盘与回测各自实际生效的参数"，运行前核对（Agent 化 A-2）；③ 在 README/日报里明确标注真实盘成交假设。
- **影响**：避免"回测好看、真实盘另一个样子"的归因错误；改动小。

### P0-3 🔴（本轮新发现）回撤告警永不触发——`_maybe_notify` 传参 bug，`max_drawdown_alert` 形同虚设

- **位置**：`run.py::_maybe_notify`（真实代码第 ~640 行）／`aitrader/notify.py::check_alerts`
- **现状（已用真实代码核实）**：`_maybe_notify` 调用 `check_alerts(ok, error, [snaps[-1]], len(recent_days), idle_days=n, max_drawdown=...)` —— 只传了**单个最新快照** `[snaps[-1]]`，而 `check_alerts` 内部：
  ```python
  if snapshots:
      peak = max(s["total_assets"] for s in snapshots)   # = snaps[-1]
      last = snapshots[-1]["total_assets"]                # = 同一值
      if peak > 0 and (peak - last) / peak >= max_drawdown:  # (x-x)/x == 0，永不触发
  ```
  → `peak == last`，`(peak-last)/peak ≡ 0`，任何 `max_drawdown_alert > 0` 都**永远不会**满足 → 净值回撤告警逻辑是死代码。
- **建议**：① `_maybe_notify` 改传**全部** `snaps`（`check_alerts(ok, error, snaps, ...)`），让 `check_alerts` 自己算 `max`/`last`；② 给 `check_alerts` 加一个单测：构造"先涨后跌"快照序列断言能触发回撤告警（现在测试只覆盖"失败/无成交"，回撤分支从未被真实验证）；③ 顺带确认 `notify.enabled` 默认关 + `webhook_url` 空静默（真实 config 已确认 `enabled:false`）。
- **影响**：用户配了 `max_drawdown_alert` 想被通知"净值回撤超阈值"，实际永远收不到——无人值守最依赖的"风控哨兵"失效。改动极小（1 行 + 1 测试），本轮应随 P0-1 一起做。

---

## 3. 🟡 P1（可靠性 / 评测科学性 / 自我改进地基）

### P1-1 回测结果无结构化留档（run ledger）——"自动 Agent 自我改进"的地基
- **位置**：`run.py::run_backtest`／`aitrader/backtest.py`／`aitrader/database.py`
- **现状**：每次回测只打印 + 生成 HTML，**配置、指标、基准、Rank IC、API 调用量都不落库**（真实库 `backtest_runs` 表不存在、无 `--list-runs`）。跑过的实验无法历史对比，"哪次改了 system/参数得出哪个结果"全靠记忆，多 AI 评审协作时容易对不上账。
- **建议**：新增 `backtest_runs` 表（run_id、时间、区间、config 快照 JSON、各引擎 metrics、bench、rank_ic、api_calls/cache_hits、fill/adjust/feature/market_env 等实验参数），每次回测写入；`--list-runs` 可查、`--diff-runs A B` 可对比；回测落库决策顺带回填 `execution_result`。
- **影响**：把"评测"从一次性动作变成可追踪记录，是自动调参/自动对比的必需品（衔接 Agent 化 A-1）。

### P1-2 无 `--health` 自检——"环境坏了"要等人肉眼发现
- **位置**：`run.py`／`aitrader/notify.py`
- **现状**：无自检命令；"连续 N 天 `last_run` 陈旧 / 未跑"不在告警条件里。定时任务 + 启动项失败、网络永久断开、Key 失效等，只能靠用户每天开日报发现。
- **建议**：加 `--health`（网络连通 / 交易日历可加载 / key 非空 / config 一致性 / bars 新鲜度 / `last_run` 新鲜度 / 各账户最近快照时点），返回非 0 码供定时任务/监控抓取；`check_alerts` 增加 `last_run_stale_days` 阈值。
- **影响**：无人值守 Agent 的"体温计"。

### P1-3 单一数据源单点故障——新浪挂了整日不跑
- **位置**：`aitrader/datasource.py::AkShareDataSource`（真实文件已核实：历史/实时/政策全走新浪，无腾讯 `qt.gtimg.cn`/东财 fallback）
- **现状**：行情/实时/政策全走新浪（`fund_etf_hist_sina`/`hq.sinajs.cn`/`stock_info_global_cls`），无第二源。新浪接口偶发不可达（历史已验证：梯子/网络抖动 WinError 10060）→ 当日剔除/跳过。
- **建议**：`fetch_daily_bars` 主源失败后按配置 fallback 到腾讯 `qt.gtimg.cn`（日线）或东财；实时行情同理（`qt.gtimg.cn` 可用已实测）。多源是无人值守连续性的兜底。
- **影响**：减少"少跑一天"的敞口；改动集中在 datasource。

### P1-4 交易日历无本地缓存 + 无重试——偶发失败 = 少跑一天
- **位置**：`aitrader/datasource.py::_load_trade_calendar`（真实代码已核实：进程启动拉一次，失败置 `calendar_ok=False` → 工作日被保守跳过；成功也只存内存 `_TRADE_CALENDAR`，进程退出即丢，无重试包装）
- **建议**：`retry_call` 包装 + 成功落盘 `data/trade_calendar.json`，次日启动先读缓存、失败才联网；缓存附有效期。
- **影响**：日历抖动的漏跑归零。

### P1-5 政策文本不归档——ai_policy 引擎"烧 API 但无法评测"
- **位置**：`aitrader/batch.py::_fetch_policy`／`aitrader/engines/deepseek.py`
- **现状**：`ai_policy` 真实盘每天拉政策喂 AI（并计入 API 调用），但 `policy_text` 不落库（真实库无 `policy_archive` 表）→ 无法回测"政策信息对决策是否有价值"（PP-7 前置），也复现不了当日决策输入。
- **建议**：新增 `policy_archive` 表（date、items JSON），`_fetch_policy` 落库；攒 3 个月后做"政策版 vs 纯价版"回测 A/B；同时日报显示"今日参考政策 N 条"。
- **影响**：让政策版引擎从"黑盒烧钱"变成"可评测资产"。

### P1-6 AI 成本无累计记账——"烧钱"看不见总量
- **位置**：`aitrader/engines/deepseek.py::api_calls/cache_hits`／`run.py::write_last_run`
- **现状**：N-8 只有**进程内**计数 + 写 `last_run.json`（单日），无持久化累计；跨天/跨回测累计 API 次数、估算成本不可见。
- **建议**：`batch_runs`/新表 `api_usage`（date、engine、api_calls、cache_hits）逐日累计；日报/`--health` 展示"本月 API 调用/估算成本"。
- **影响**：长期运行的成本可观测，防"缓存失效全量重跑"类意外烧钱。

### P1-7 idle 告警用日历天近似交易日 + 长期空仓误报
- **位置**：`run.py::_maybe_notify`（`cutoff = date - timedelta(days=n*2)`）／`aitrader/notify.py::check_alerts`
- **现状**：① 用 `n*2` 天近似 N 个交易日不精确（长假偏差大）；② `trades_count==0` 无条件告警，对本来就空仓的保守策略是持续误报噪音。
- **建议**：用 `is_trading_day` 过滤统计最近 N 个交易日成交；空转告警区分"有持仓但无成交"（真异常）与"本来就空仓"（降级为提示）。
- **影响**：告警可信度。

### P1-8 config.json 未暴露全部实验参数——改真实盘风控只能靠 CLI
- **位置**：`config.json`／`aitrader/config.py::Settings`（真实文件已核实：`config.json` 缺 `risk.slippage_bps/stop_loss_pct/take_profit_pct/min_confidence_buy` 以及顶层 `temperature/system_prompt_extra/max_buy_count`）
- **现状**：`config.json` 缺上述字段，默认值全 0 → 真实盘与回测可漂移（P0-2）。
- **建议**：新参数全部进 `config.json`（并入 risk 块 / 顶层），CLI 覆盖时打印"已覆盖 config 默认"。

---

## 4. 🟢 P2（工程体验 / 可运维性）

| # | 类别 | 位置 | 问题（现状） | 建议 |
|---|---|---|---|---|
| P2-1 | 存量 | `run.py` | `--db` 在批处理/回测语义混用（真实 `run_backtest` 用 `Path(args.db)`），误传会污染每日账本 | 回测改 `--bt-db` 独立参数 + "确认覆盖"保护 |
| P2-2 | 存量 | `aitrader/database.py::_connect` | 无 WAL（真实库无 `PRAGMA journal_mode=WAL`）、`trades/decisions` 无 account_id 索引（无 `CREATE INDEX`）、每操作重开连接 | `PRAGMA journal_mode=WAL` + 索引 + 长连接 |
| P2-3 | 存量 | `aitrader/reporter.py`（真实 `plot_compare`/`plot_backtest_curves` 两处 rcParams） | `Microsoft YaHei` 硬编码，跨平台乱码 | `matplotlib.font_manager` 探测可用中文字体 |
| P2-4 | 存量 | `aitrader/reporter.py::plot_compare` | 日报资金曲线仍是**绝对"总资产（1e6 轴）"**（用户贴图 1 正是此图），与回测净值图口径不一 | 统一净值起点=1（与 `plot_backtest_curves` 一致），叠加回撤子图 |
| P2-5 | 存量 | `.env`／`load_settings` | key 为空/占位无启动校验（真实代码：批处理模式 `select_daily_engines` 缺 key 只回退 rule 提示，不报错） | 启动校验并给出明确提示（回测已做显式报错，批处理补上） |
| P2-6 | 新 | `run.py` | 回测参数覆盖 config 时无冲突告警（如 `--fill close` 但 config `next_open`） | 覆盖时打印"用 X 覆盖 config 的 Y" |
| P2-7 | 新 | `scripts/` | 脚本（bench_metrics/check_daily/corr_analysis/stop_grid）无统一入口文档、无测试 | 加短注释头（用法/依赖/是否烧 API），纳入 README 工具清单 |
| P2-8 | 新 | `aitrader/batch.py::_fetch_and_store` | 每天全量拉 lookback+45 天（N-11 只兜底不提速） | 增量拉取（从 `bars` 表最新日期起）+ 收盘后批量预取缓存 |
| P2-9 | 新 | `aitrader/notify.py`／config.notify | 通知渠道单一（webhook/Server酱），且 notify 默认关 | 支持多渠道（Server酱→微信 / 邮件 / 钉钉），加"日报自动推送"与失败升级 |

---

## 5. Agent 化专项（从"每天定时跑"升级为"自动量化 Agent"）

> 以上 P0/P1/P2 是"把机器修稳"；本节是"让机器能自我改进、可被自动管理"。

- **A-1 实验台账 → 自动对比循环**：P1-1 的 `backtest_runs` 落库后，加 `--list-runs` / `--diff-runs A B`，并让 `scripts/bench_metrics.py` 自动汇总"近 N 次实验"对比表；Agent 每周自动复跑基线 + 输出"收益/回撤/夏普/成本"建议报告（可人审后落地）。
- **A-2 `--print-config` 一键核对**：打印真实盘与回测各自实际生效的 fill/adjust/feature/market_env/slippage/stop-loss/temperature 等，防口径漂移（P0-2 落地件）。
- **A-3 日报升级为"预测 vs 实际"评分卡**：在现有（今日决策/基准对照/数据截至/归因/分桶胜率/Rank IC）基础上，加 **AI 成本累计**（P1-6）与"近 20 交易日 Rank IC 滚动值"，让 Agent 的每日决策可被自动打分。
- **A-4 自动风控重估**：`scripts/stop_grid.py` 网格脚本已具备，可改为"每周自动跑一次止损/止盈网格（rule 免费）+ 输出是否建议开启"；AI 引擎的重估需烧 API，排期。
- **A-5 通知分级**：P0（数据挂/运行失败）→ P1（净值回撤/连续无成交/空仓误报修正）→ P2（每日日报），多渠道推送；`--health` 结果可被通知/监控消费。**前置：先修 P0-3 让回撤告警真的能触发。**

> 预测质量轨剩余项（PP-7 政策归档回测、PP-8 标的池扩池）在 `docs/PREDICTION_IMPROVEMENTS.md`，需烧 API / 攒数据，不在本文件重复。

---

## 6. 落地优先级建议

**第一梯队（改动小、直接堵数据可信度/无人值守口子，建议立即做）**
1. P0-1 `--force` 幂等重定义（清当日留痕再跑 / trades 唯一约束 + upsert）——约 1~2 小时
2. **P0-3 回撤告警传参修复 + 单测（本轮新发现，1 行改动）——约 0.5 小时**
3. P1-1 回测 run ledger 落库 + `--list-runs`——约 2~3 小时
4. P1-2 `--health` 自检 + last_run 陈旧告警——约 1 小时
5. P1-3 行情多源 fallback（腾讯备用）——约 1~2 小时

**第二梯队（评测科学性 / 成本可观测 / 防漂移）**
6. P1-4 交易日历本地缓存 + 重试
7. P1-5 政策归档（攒 3 个月后可回测）
8. P1-6 AI 成本累计记账 + 日报展示
9. P0-2 + P1-8 config risk 补全 + `--print-config`

**第三梯队（工程打磨 + Agent 化）**
10. P2-1~P2-9 逐项（`--bt-db` / WAL / 字体 / 净值轴 / key 校验 / 冲突告警 / 脚本文档 / 增量拉取 / 通知）
11. A-1~A-5 自动对比 / 核对 / 评分卡 / 风控重估 / 通知分级

---

## 7. 验收清单 + 落地记录

### 已验证（v0.1~v0.18 已落地，2026-08-12 真实代码复核 + 184 用例全过）

- [x] 日志落盘 `data/logs/app.log` 5MB×5 轮转／`last_run.json` 运行留证（异常保留上次正常）／单实例文件锁
- [x] 同日幂等（快照 + `batch_runs` 只认 done + `last_interest_date` 计息幂等）／交易日判断／`--date` 回放无前视／`source`(real/replay) 隔离
- [x] 行情硬校验（bars[-1]==决策日）＋逐标的陈旧剔除＋实时补全（新浪 hq.sinajs.cn）＋政策 15:30 截断防前视＋政策决策日过滤
- [x] DeepSeek 单条解析容错／语义校验留痕（validation + already_holding）／网络重试（retry_call）／响应缓存原子写＋缓存键含 model/temperature/system/prompt
- [x] 回测 O(log N)、报表只渲染本轮引擎、基准佣金同口径、`fill_mode:next_open` + `adjust:hfq` 默认、`--fill/--commission-mult/--adjust`、回测 end 默认 `_last_closed_trading_day`（A-5，真实代码已接线）
- [x] 双 bars（真实盘特征复权，N-1）／特征注入 + 市场环境注入（config 已开 true）／置信度门槛／历史盈亏反馈／止损止盈／滑点／现金生息
- [x] 真实盘 Rank IC 回填 + 日报（分桶胜率/归因复盘/基准对照/数据截至/今日决策/货基利息）
- [x] 引擎级异常隔离（A-2）／收盘守卫（A-3）／未来日期拒绝（A-4，`--date` 用 `_date_type`）
- [x] `--catch-up` 补跑 + **已接线到启动项（install_task.ps1 含 --catch-up）**／通知（N-10，跨引擎判定）／bars 缓存兜底（N-11）／API 调用计数（N-8，last_run.api_stats）
- [x] 归因净佣金口径（P1-1）、实时行情重试（P1-3）、`--force` 计息幂等 + batch_runs（v0.17 P0-1 部分）

### 验收清单（本轮待落地，落地后逐项打勾）

- [ ] `--force` 不再重复成交（清当日留痕重跑 / trades 唯一约束 + upsert）—— P0-1
- [x] **回撤告警能真正触发（`_maybe_notify` 传全量快照 + 回撤单测）—— P0-3（v0.19 已落地）**
- [ ] 真实盘与回测参数/成交口径可一键核对（`--print-config` / risk 块补全）—— P0-2 + P1-8
- [ ] 回测 run ledger 落库 + `--list-runs`/`--diff-runs`—— P1-1
- [ ] `--health` 自检 + last_run 陈旧告警—— P1-2
- [ ] 行情多源 fallback（腾讯备用）—— P1-3
- [ ] 交易日历本地缓存 + 重试—— P1-4
- [ ] 政策归档（policy_archive）—— P1-5
- [ ] AI 成本累计记账 + 日报展示—— P1-6
- [ ] idle 告警精度（按交易日 + 空仓区分）—— P1-7
- [ ] P2-1~P2-9 逐项 + Agent 化 A-1~A-5——第三梯队

### 落地记录（v0.1~v0.18 里程碑，详见仓库 git log）

- **v0.1~v0.2（08-09）**：日志/last_run/单实例锁/语义校验留痕；ai_policy 批处理过滤、行情失败跳过、DeepSeek 解析容错、网络重试、回测 O(log N)。
- **v0.3~v0.4（08-09）**：回测报表只渲染本轮引擎、数据新鲜度守卫、逐标的陈旧剔除 + bar_date、`batch_runs` 防崩溃重复成交、execution_result 回填、日期参数校验。
- **v0.5~v0.7（08-09）**：成交假设校准（PP-1）、system 三段式 + 温度/模型参数化 + 缓存键修正（PP-3）、特征注入（PP-2）、置信度门槛 + Rank IC 闭环（PP-4）、复权接入回测、网络硬超时 + IPv6 修复。
- **v0.8~v0.9（08-10）**：实时行情补全（治新浪滞后 1 日）、政策 15:30 截断防前视 + 决策日过滤、行情"当日硬校验"。
- **v0.10~v0.12（08-10）**：真实盘接入特征注入（A-1）、未来日期拒绝（A-4）、引擎异常隔离（A-2）、收盘守卫（A-3）、回测 end 默认最近已收盘（A-5）、滑点（P2-2）、真实盘 Rank IC 回填（B-1）、日报升级（B-2）、`batch_runs` 只认 done（N-2）、`source` 隔离（N-12）、信心分桶（N-4）。
- **v0.13~v0.14（08-11）**：双 bars 真实盘特征复权（N-1）、成交口径说明（N-3）、归因标签 + 复盘（N-5）、`--market-env` 接线（N-6）、API 记账（N-8）、`--catch-up`（N-9）、通知（N-10）、bars 缓存兜底（N-11）。
- **v0.15（08-11）**：烧 API A/B——市场环境注入开启（夏普 0.98→1.57）、历史盈亏反馈可选、止损/止盈网格无有效组合保留默认关、标的池相关性 0.75~0.87 高度冗余（完整结论在 PREDICTION 文档）。
- **v0.16（08-12）**：现金生息（货基假设，空仓也被正确计价）。
- **v0.17（08-12）**：P0-1 计息幂等（last_interest_date）、P0-2 告警跨引擎、P0-3 日报数据截至 + 陈旧高亮、P0-4 启动项接 `--catch-up`、P0-5 IC 只收实际成交；P1-1 归因/浮盈净佣金口径、P1-2 `already_holding`、P1-3 实时行情重试、P1-6 last_run 保留上次正常。
- **v0.18（08-12）**：日报展示货基利息累计（快照 interest 列 + 卡片单列）。

### v0.6 评审说明（2026-08-12，主 agent 亲自终端读真实磁盘代码）

- v0.5 的 P0/P1/P2 全部核实属实（trades 无唯一约束、config risk 块 3 字段、无 backtest_runs、无 --health、单一数据源、日历无缓存、无 policy_archive、无 api_usage、idle 用 n*2 天、plot_compare 1e6 轴、无 WAL/索引、字体硬编码）。
- **本轮新增 P0-3（回撤告警永不触发）**：`_maybe_notify` 传 `[snaps[-1]]` 单元素快照 → `check_alerts` 的 `peak==last` → `max_drawdown_alert` 永不满足（真实代码逐行核实）。修复 1 行 + 补回撤单测。
- 用户贴图确认：图 1 为 `plot_compare` 的绝对资产 1e6 轴（P2-4 属实且肉眼可见）；图 3 为 `plot_backtest_curves` 净值起点=1 回撤双面板（已按 P2-4 目标口径，说明回测侧已达标、日报侧待统一）。


### v0.19 落地记录（2026-08-12，主 agent）

- **P0-3 回撤告警修复（1 行 + 3 测试）**：_maybe_notify 改传全量 snaps（原传 [snaps[-1]] 单元素 → check_alerts 里 peak==last → max_drawdown_alert 永不满足，**该 bug 是 v0.17 改 P0-2 时引入的回归**）。新增 	ests/test_v19.py 3 条（全量快照回撤触发 / 单元素不触发 / _maybe_notify 集成触发推送）。测试 187 全过。