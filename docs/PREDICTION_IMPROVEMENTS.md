# PREDICTION_IMPROVEMENTS — 自动量化 Agent · 预测/决策质量改进清单

> 定位：把「每日定时仿真」升级为「可自主评估、自我改进的自动量化 Agent」。
> 预测/决策质量为主线，辅以支撑"无人值守自动跑"的可靠性项（完整工程清单见 `docs/IMPROVEMENTS.md`，二者分工不重复）。
> 铁律：**所有"变好了"的结论必须来自真实回测 A/B，禁止凭感觉、禁止虚构收益。**

## 状态快照（2026-08-10）

- 已落地并验证：PP-1 成交假设校准（close/next_open）、PP-2 特征注入+复权、PP-3 system 三段式+缓存键修正、PP-4 置信度门槛+Rank IC 闭环、数据时效全链路（v0.8 实时补全 / v0.9 政策截断+行情硬校验）、A-1~A-5 可靠性、P2-1/P2-2 配置对齐与滑点、B-1/B-2 真实盘校准与日报（v0.10/v0.11，测试 136 全过）。
- **当前最优配置已全部落地**：回测默认 `fill_mode:next_open` + `adjust:hfq`（P2-1）；真实盘 `feature_inject:true`（A-1，config.json）——**但注意 N-1：真实盘特征实际基于未复权价计算（A-1 只做了一半）**，详见待办。
- A/B 结论（已固化）：最优 = 原 system + `--feature-inject --adjust hfq`（AI 1 年 +21.7%、夏普 1.77、跑赢买入持有 +19.58%）；置信度门槛无预测信息（Rank IC≈0）；"大胆化"变体一失败已回滚。
- ⚠️ LLM 记忆局限：回测区间（2025-06~2026-08）可能被模型"记住"（开卷考试）→ 回测绝对收益可能高估、不可外推；A/B 相对对比仍有效；**真实盘才是最终考场**（B-1 已为其装评分表）。
- 🔎 **本轮评审（2026-08-10，主 agent 亲自读码）**：用户要把系统升级成"自动量化 Agent"。发现 12 条新缺口（N-1~N-12，其中 N-3/N-11/N-12 低优先级）+ 4 条存量（PP-5~PP-8）。核心判断：**工程可靠性已达标，重心转到 ① 真实盘口径一致性（N-1）② 无人值守自愈与可观测（N-2/N-8/N-9/N-10）③ 决策质量闭环（N-4/N-5/B-3 收口）**。

---

## 待落地清单（按 P0 / P1 / P2 分级）

> 已落地项（PP-1~PP-4、复权接入、Rank IC 闭环、网络修复、A-1~A-5、B-1/B-2、P2-1/P2-2）见下方"落地记录"；此处仅列**未落地 + 本轮新发现（N-1~N-12）**。

### P0（真实盘口径一致性 / 无人值守可靠性，建议最先处理）

- [x] **N-1｜真实盘特征注入用的是未复权行情——A-1 只做了一半（【新发现】）**
  - 现状：`config.json` 已 `feature_inject:true` + `adjust:"hfq"`，回测走 `adjust=hfq`；但 `batch.py::_fetch_and_store` 调 `fetch_daily_bars` 时**没有传 `adjust`** → 真实盘喂给 `compute_features` 的 bars 是**未复权原始价**。
  - 问题：除权日价格跳空会让 MA/RSI/距高/动量等特征失真（与回测口径不一致）；A-1 原意"特征用复权价、估值/成交用原始价"只实现了一半——用户以为真实盘跑的是 A/B 最优配置，实际是"特征失真版"。
  - 改法：批处理对每个标的拉复权价（复用 `adjust="hfq"`）专供特征计算，估值/成交仍用原始价（batch 内部对 bars 做 `compute_adjusted_bars` 算特征，再传原始价给估值/成交即可）；口径差异写进 README。
  - ✅ **v0.13 已落地（2026-08-11，双 bars 方案）**：`batch._fetch_and_store` 返回 `(bars_map 原始, adjusted_map 复权, failed)`；新增 `_adjusted_bars_map`（本地 `compute_adjusted_bars` + 分红缓存生成复权，**零额外网络**，失败降级原始不中断）；`DecisionContext` 新增 `adjusted_bars` 字段；`DeepSeekEngine._feature_bars` 优先复权算特征（feature_inject + market_env），缺失/为空回退原始；**估值/成交/价格展示仍用 `bars` 原始价**；`_calibrate_forward_returns` 改用复权价回填（含分红口径，与回测 hfq 一致）。实现说明：真实盘 `fetch_daily_bars` 历史接口本就无 adjust 参数，故"本地生成复权"是唯一可行路径（比改拉取更稳，且不增网络调用）。
  - 验收：pytest 断言批处理喂给特征的是复权价、估值/成交用原始价；除权日前后特征无跳变。

- [x] **N-2｜崩溃遗留的 `batch_runs.status='running'` 会把账户卡死在"假跳过"（【新发现】）**
  - 现状：`_run_engine` 在 `begin_batch_run`（写 running）之后、`add_snapshot` 之前崩溃（如 `execute_decisions` 抛异常 / 落库失败），该日留下 `(account_id,date,'running')` 且无快照；`database.has_batch_run` 不区分状态 → 当日再跑（启动项+定时任务双触发/手动）直接跳过，且 `has_snapshot` 为假时返回 `initial_capital` 当"总资产"，**掩盖已有持仓与成交**；该日也永远不会被自动补做。
  - 问题：无人值守下崩溃一次就产生"假跳过 + 假净值"，且无法自愈。
  - 改法：① `has_batch_run` 只认 `status='done'` 才算已处理；② `begin_batch_run` 对同日的 running 允许覆盖重试；③ 无快照的 running 一律允许重跑。
  - ✅ **v0.12 已落地（2026-08-10）**：`has_batch_run` 改为 `AND status='done'`；`begin_batch_run` 用 `ON CONFLICT DO UPDATE ... WHERE status='running'`（done 不被重试打回）；崩溃遗留 running 无快照 → 重跑自动补齐快照。测试 `test_stale_running_gets_rerun` / `test_begin_retry_keeps_done` 覆盖；v0.4 旧测试同步更新到新语义。

- [x] **N-3｜真实盘成交口径与 `fill_mode:next_open` 语义混淆（【新发现】，低危但需写清）**
  - ✅ **v0.14 已落地（2026-08-11）**：`last_run.json` 新增 `fill_note`（真实盘=决策日收盘价成交、滑点默认0；回测 next_open+slippage 是更严苛口径、真实盘表现不应优于回测）；README 已同步口径说明。
  - 现状：`config.json` `fill_mode:"next_open"` 是回测假设（PP-1）；真实盘批处理 `execute_decisions` 不带 `fill_prices` → **实际按当日收盘价成交**，fill_mode 对批处理完全无作用。
  - 问题：用户/维护者看到"最优配置 = next_open"会误以为真实盘也在等次日开盘成交（实盘其实收盘即成交）；口径不写清易误读日报与回测对比。
  - 改法：README + `last_run.json` 明确标注"真实盘 = 收盘价成交（滑点默认 0）；回测 next_open + slippage 是更严苛口径，真实盘表现应不优于回测"；`last_run.json` 加 `fill_note`。
  - 验收：pytest 断言批处理成交价 = 决策日收盘（现有语义）；日志/文档有口径说明。

### P1（决策质量 / 评测闭环）

- [x] **N-4｜真实盘 Rank IC 只有累计一个数，缺"按置信度分桶胜率"与滑窗趋势（【新发现】）**
  - 现状：B-1 已回填 `decisions.forward_return`；`reporter.build_daily_report` 只算一个累计 `rank_ic`（样本 ≥5 才显示）。
  - 问题：累计 IC 被早期样本主导；且"AI confidence 有没有用"更直观的呈现是**分桶胜率**——把已校准 buy 按 confidence 分桶（如 <0.6 / 0.6~0.7 / >0.7）统计每桶"正收益占比/平均收益/样本数"，用户一眼看出"高信心是否真的更准"（当前 AI confidence 集中 0.6~0.7，分桶正是为此设计）。
  - 改法：日报加"confidence 分桶胜率"小表（复用已回填 decisions）；可选加近 20 笔滑窗 IC 趋势。
  - ✅ **v0.12 已落地（2026-08-10，分桶部分）**：日报新增"按信心分桶（正收益占比）"表，桶 `0~0.6` / `0.6~0.7` / `0.7+`，独立于 IC 样本门槛（有样本即渲染）。滑窗 IC 趋势留待后续。

- [x] **N-5｜决策理由自由文本，缺结构化归因标签——无法自动复盘（【新发现】）**
  - ✅ **v0.14 已落地（2026-08-11）**：提示词要求 reason 以 [趋势]/[回调]/[政策]/[超买]/[超卖]/[其他] 开头（system + 输出契约双处）；新增 `aitrader/attribution.py`（`parse_tag` + `attribute_trades` 按买入理由标签聚合已平仓配对盈亏）；日报新增"归因复盘"表；未带标签归入"其他"不崩溃。
  - 现状：`decisions.reason` / `trades.reason` 是自由文本，无可枚举的"这单是趋势/回调/政策/超买哪个理由"维度。
  - 问题：PP-6（历史盈亏反馈）是"喂给模型的复盘"，但在此之前缺"给用户的自动复盘"：哪类理由的买入后来亏了、哪类赚了，完全无法统计。
  - 改法：提示词要求 `reason` 以标签开头（如 `[趋势]`/`[回调]`/`[政策]`/`[超买]`/`[其他]`，输出契约加枚举说明）；新增 `scripts/attribution.py`（或 reporter 内）按标签聚合已平仓交易盈亏，输出到日报/周报。纯 prompt + 统计，零 schema 变化。
  - 验收：pytest 断言标签解析与聚合正确；未带标签的 reason 归入"其他"不崩溃。

- [x] **N-6｜B-3 市场环境注入"落地一半"：已实现但从未 A/B，真实盘也没接线（【新发现】）**
  - ✅ **v0.14 已接线（2026-08-11）**：`--market-env` 对批处理也生效（与 feature-inject 对称）；config.json 暴露 `market_env_inject:false`（默认关）。
  - ✅ **A/B 结论（2026-08-11，AI 特征+复权，2025-06~2026-08）**：基线（无环境）**+13.40%/夏普0.98/回撤10.04%/4笔** vs +市场环境 **+9.52%/夏普1.57/回撤2.86%/7笔**。**满足开启标准（夏普≥基线+0.1）**：收益略降（-3.9pp）但回撤大减（-7.2pp）、夏普+0.59。⚠️ 样本少（7笔），置信度有限；真实盘为"更稳而非更赚"，取舍见本轮实验结论。
  - 现状：`_format_market_env` 纯函数已实现且有单测，`Settings.market_env_inject` 默认 False、config.json 无字段、`--market-env` 只在回测分支生效、批处理不读。
  - 问题：一个"可能有用也可能有害"的提示词特性挂在中途——既没被验证（无 A/B 结论），又无法在真实盘开启。
  - 改法：① 先做小样本回测 A/B（A=现状 vs B=+市场环境行，同 system/特征/复权，同区间，≈几百次 API 一次计费）出结论；② 若 B 样本外 Sharpe ≥ A+0.1 再在 config 暴露 `market_env_inject` 给真实盘，否则按"无增量"如实记录并保持默认关。
  - 验收：pytest 断言 prompt 含/不含市场环境行受开关控制（已有）；A/B 结论写入本文档。

- [x] **PP-5｜止损/止盈/回撤熔断网格——诚实检验"止损是否真的帮 AI"（【低置信探索】，存量未落地）**
  - ✅ **v0.15 已落地（2026-08-11）**：`portfolio.apply_stop_rules`（现价≤成本×(1-止损) → 强制整仓卖 `[stop_loss]`；≥成本×(1+止盈) → 强制卖 `[take_profit]`，先于模型决策执行）；`RiskConfig.stop_loss_pct/take_profit_pct`（默认 0 关）；batch/backtest 执行链接入；CLI `--stop-loss/--take-profit`；网格脚本 `scripts/stop_grid.py`（独立 db，rule 免费 / AI 精简档）。测试 `test_v15.py` 4 条。
  - ✅ **网格结论（2026-08-11，rule 引擎 2021-01~2024-12，12 组合）**：**无组合同时满足"夏普≥基线+0.1 且 回撤下降≥3pp"→ 按规则保留"无止损"为默认**。观察：止损各档对 rule 收益/回撤几乎无改善（趋势跟随风格，账户级回撤 20% 不因单笔止损而变）；止盈 20% 档收益明显改善（-5.7%→+4.6%）、夏普 -0.06→0.15，但回撤无下降、不满足双条件。⚠️ rule 在 2021-2024 本身跑输（震荡市），结论仅对 rule 有效；AI 止损网格（需烧 API，且止损触发会分叉计费）未做，标注为可选后续。
  - 现状：`risk.py::validate_buy`、`portfolio.py::execute_decisions` 均无止损/止盈/回撤熔断；sell 完全由模型决定；`RiskConfig` 无相关参数（IMPROVEMENTS P1-5 已立项未落地）。
  - 问题：AI 卖出滞后于价格（只在决策日表态），单边下跌可能死扛到深亏；**但止损对无固定趋势风格的 AI 是双刃剑**——固定止损可能在正常回调被迫离场、趋势恢复后再追高，形成"割肉+追高"双边损耗。**故绝不默认止损有用，必须网格实测**；回撤熔断（账户级冷却）相对安全。
  - 改法：① `RiskConfig` 加 `stop_loss_pct=0.0`（0=关闭）、`take_profit_pct=0.0`、`max_drawdown_halt=0.0`、`halt_cooldown_days=5`；② 新增 `portfolio.py::apply_stop_rules(state, prices, risk, date) -> (state, forced_trades, halted)` 纯函数：持仓现价 ≤ 成本×(1-止损) → 强制整仓卖；≥ 成本×(1+止盈) → 止盈；账户回撤 ≥ 熔断 → 全部清仓 + 冷却标记（`batch._run_engine` 与 `backtest` 回放循环读冷却态，强制 hold N 日）；③ 在 `execute_decisions` 之前调用。
  - 实验设计：网格 stop_loss ∈ {0,5%,8%,12%} × take_profit ∈ {0,10%,20%}，rule 与 ai 分别全组合回测同区间（2021-01~2024-12）；熔断 20%。判定：某组合样本外 Sharpe ≥ 无止损基线+0.1 **且** max_drawdown 下降 ≥3pp 才记为有效；**若所有止损档都不优于无止损，如实保留"无止损"为默认并记录结论**。多重比较防护：2025-01~2026-06 留作最终确认样本，网格只在训练段选优。
  - 风险与副作用：网格最优易过拟合（需留样本外）；止损+佣金在震荡市双杀；冷却期可能错过反弹。改动集中在 risk/portfolio 纯函数，schema 无变化。
  - 验收：pytest 断言触发止损生成强制 sell 成交且 reason 含 `[stop_loss]`；冷却期内引擎决策被替换为 hold；网格实验脚本输出全组合指标表并标注最优，供人审阅而非自动采纳。

- [x] **PP-6｜历史交易盈亏反馈——让 AI 学会"复盘"（【低置信探索】，存量未落地）**
  - ✅ **v0.15 已落地（2026-08-11）**：`attribution.closed_trade_pairs`（FIFO 配对已平仓明细，兼容 dict/Trade）；`DecisionContext.recent_closed_trades` + `feedback_n`（默认 0 关）；`DeepSeekEngine._build_prompt` 复盘节（近 N 笔已平仓：symbol/买卖日/价/盈亏/当时理由，仅实际成交、无前视）；`batch`/`backtest` 各自注入；CLI `--feedback N`。测试 `test_v15.py` 9 条。
  - ✅ **A/B 结论（2026-08-11，AI 特征+复权，2025-06~2026-08）**：基线（无反馈）**+13.40%/夏普0.98/回撤10.04%/4笔** vs +反馈5笔 **+10.34%/夏普1.45/回撤4.34%/11笔**。**满足提升标准**：成交更活跃（4→11笔）、夏普+0.47、回撤减半（-5.7pp）、收益略降。⚠️ 样本少（11笔），置信度有限。
  - 现状：`deepseek.py::_build_prompt` 只含当前持仓成本与浮盈，无历史已平仓交易及盈亏；模型每次决策"失忆"，重复追高/死扛模式无法自我纠正。
  - 问题：无行为反馈 = 模型在"无记忆的重复试错"。注入"近 N 笔已平仓交易 + 盈亏 + 当时理由"，让模型建立"行为→结果"关联，可抑制重复犯错；数据已在 trades 表，边际成本低。
  - 改法：① `_build_prompt` 追加"近期已平仓交易（复盘参考）"：取近 `feedback_n`（默认 5）笔**已平仓**（有 sell）的 (symbol, buy日期/价, sell日期/价, pnl_pct, buy_reason) 倒序；② `DecisionContext` 增加 `recent_closed_trades: list[dict]`（`batch._run_engine` / `backtest` 各自注入）；③ 缓存键自动含该节（prompt 变化）→ 不串缓存；④ 铁律：只取**实际成交** trades，不含被风控拒绝/未成交的决策。
  - 实验设计：A=无反馈 vs B=近 5 笔 vs C=近 10 笔，同区间。判定：B/C 样本外 profit_factor ≥ A+0.1 且"重复亏损模式"下降；控制近因偏差：反馈只含已发生（≤当日）交易，回测无前视。
  - 风险与副作用：反馈近因偏差；"过度反思"致成交数下降；prompt 变长。改动小（prompt 一节 + context 一字段）。
  - 验收：pytest 断言反馈只含已平仓实际成交、不含未来成交（回测无前视）；条目 ≤ 配置上限；`feedback_n=0` 关闭。

- [ ] **PP-7｜政策时效与归因——先让"政策版"可回测（【低置信探索】，存量未落地）**
  - 现状：`batch.py::_fetch_policy` 拉政策但不落库；`AkSharePolicySource.fetch_macro_news` 返回 `"标题｜内容"` 整段（未取财联社电报时间戳），`_build_prompt` 整段塞入最多 8 条；`backtest.py` 中 `policy_text=""` → **ai_policy 永远无法在回测中验证**。
  - 问题：政策版与纯价格版孰优完全不可知（评测盲区）；"当日 15:30 后发布的政策"若塞进"当日 15:30 决策"即构成前视；无归因（政策→标的/板块映射缺失）导致模型无法判断"哪条政策影响哪个持仓"。
  - 改法：① 新增 `policy_archive` 表（date, time, title, content, hit_keywords），`_fetch_policy` 落库；`AkSharePolicySource` 增取时间戳列（调研 `ak.stock_info_global_cls` 列名，取不到用拉取日期兜底）；② `_build_prompt` 政策节改结构化单行 `[09:35|央行|降准0.5pp|摘要]`，提示"判断是否已被价格反映（预期差），只对超预期的给行动；与你持仓无关则忽略"；③ `Backtester` 增加 `policy_by_date: dict[date, str]`（从 archive 按日期回放），决策日只注入 ≤ 当日 15:30 发布的历史政策（无前视）；否则维持 `policy_text=""` 并如实标注"政策版未验证"。
  - 实验设计：ai vs ai_policy（同日历史政策注入），区间取 archive 有数据后（先积累 3 个月再评估）。判定：ai_policy 样本外 Sharpe ≥ ai+0.1 才认定政策有增量价值；若 <0.1 如实报告"政策无增量，可能为噪音"，并降级为不启用。
  - 风险与副作用：政策回放依赖 archive 数据积累（新功能，先落数据层）；政策时间与 15:30 决策的先后边界需严格校验（前视风险）；关键词过滤漏报/误报影响归因。
  - 验收：pytest 断言 archive 落库且含时间戳；回测注入的政策日期 ≤ 决策日 15:30；`policy_text` 结构化条目含时间/关键词/摘要。

### P2（自动 Agent 化 / 运维可观测，探索性）

- [x] **N-8｜每日批处理无 API 成本/调用量记账（【新发现】）**
  - ✅ **v0.14 已落地（2026-08-11）**：`DeepSeekEngine` 进程内 `api_calls`/`cache_hits` 计数（缓存未命中才计 API 会话，重试同属一次）；`last_run.json` 新增 `api_stats`（每 AI 引擎 calls/cache_hits），成本漂移可察觉。
  - 现状：真实盘每天固定 2 次 DeepSeek 调用（ai + ai_policy）+ 行情/政策拉取；`last_run.json` 不记 API 调用次数与估算费用。
  - 问题：模型被改贵 / 缓存失效 / 误配时成本漂移，无人值守下用户无法察觉。
  - 改法：`DeepSeekEngine` 加进程内 `api_call_count`（含缓存命中数）；`write_last_run` 记录 `api_calls` + `est_cost`（按次估算，写进 last_run 即可，不追求精确）。
  - 验收：pytest 断言缓存命中/未命中计数正确；`last_run.json` 含调用量字段。

- [x] **N-9｜连续缺交易日不自动补跑——启动项只补"今天"（【新发现】）**
  - ✅ **v0.14 已落地（2026-08-11）**：新增 `--catch-up [N]`（默认 5）——从目标引擎账户最近快照日的次日到昨天，逐交易日补跑（幂等，已有快照的日期自动跳过不重复成交）；无快照/无缺失时 no-op。启动项可改为 `run.py --catch-up`。
  - 现状：`install_task.ps1` = 每天 15:30 定时任务 + 登录时跑一次 `run.py`（仅当天）。若连续几天关机（周末+节假日+周一早上开机），只有"当天"被补跑，**前几个工作日缺失**（幂等不会重复成交，但缺失日期的决策/估值永远空白）。
  - 问题：真实盘账本"跳日"，资金曲线/基准对照不连续，Rank IC 样本少。
  - 改法：新增 `--catch-up [N]`：从最近一次快照日的次日开始，逐日补齐缺失交易日（内部循环 `--date`，幂等）；启动项改为 `run.py --catch-up`。
  - 验收：pytest 断言连续缺失 N 天后 catch-up 补齐全部快照且不重复成交；无缺失时 no-op。

- [x] **N-10｜无主动通知——机器坏了用户不知道（【新发现】）**
  - ✅ **v0.14 已落地（2026-08-11）**：新增 `aitrader/notify.py`（`check_alerts` 判定失败/连续无成交/净值回撤；`send_notify` 推送 Server酱/通用 webhook，失败不阻塞）；config 加 `notify` 块（默认关，`webhook_url` 空则静默）。
  - 现状：定时任务失败 / key 失效 / 行情源变化只写日志 + `last_run.json`，用户不打开就无从知晓。
  - 改法：批处理结束检查"本次失败 / 连续 N 日无成交 / 净值回撤超阈值 / AI 缓存命中率骤降"，触发 Server酱/邮件/webhook 推送（新增 `aitrader/notify.py`，失败不阻塞主流程）；`config.json` 加 `notify` 开关。
  - 验收：pytest 断言告警条件判定纯函数正确；推送失败不影响批处理结果。

- [x] **N-11｜`bars` 表只写不读 + 数据源单点（存量 IMPROVEMENTS P2-1）**
  - ✅ **v0.14 已落地（容灾部分，2026-08-11）**：行情主源失败时回退读 `bars` 表缓存兜底（当日新鲜度仍由 bar_date 硬校验把关，陈旧剔除不参与交易，不造假）。**缓存"提速"部分未做**——历史段复用缓存有前视/时效风险，评估收益 < 风险，明确不做。
  - 现状：`database.save_bars` 落库但无读取调用点；批处理/回测每次联网全量拉取，新浪单点挂了就全挂。
  - 改法：`fetch_daily_bars` 先查 `bars` 表缓存、缺的再补拉；行情源失败时 fallback 腾讯实时接口（`_fetch_realtime` 已实现，可推广为独立 fallback 源）。
  - 验收：pytest 断言缓存命中减少网络调用；主源失败降级不抛错。

- [x] **N-12｜`--date` 回放与真实盘账本混用（【新发现】，低优先级）**
  - 现状：`--date 2026-08-01` 会把历史日的快照/成交写进**真实盘同一账本**，无 `source` 字段区分 → 日报/资金曲线把"回放历史"当"真实发生"。
  - 改法：`daily_snapshots` 加 `source`（real/replay），`--date` 回放默认 source=replay；日报默认只展示 real（或标注 replay）。
  - ✅ **v0.12 已落地（2026-08-10）**：`daily_snapshots` 新增 `source` 列（含迁移 + 默认 real）；`add_snapshot` 接受 source 参数；批处理按 `date.date()==今天 → real，历史 → replay` 写入；`get_snapshots` 返回 source。同一天先 replay 后 real 时保留首次标记（ON CONFLICT 不覆盖 source）。测试 `test_snapshot_source_replay_for_history` 等覆盖。日报按 source 过滤留待后续（当前展示全部 + source 可审计）。

- [ ] **PP-8｜标的池分散与轮动——用相关性而非数量决定分散（【低置信探索】，存量未落地）**
  - 现状：池内仅 510300/588000/159915 三只高度相关宽基（科创50/创业板成长风格高度相关），`max_buy_count=2`；AI 只能在高度相关的池子里"伪选择"。
  - 问题：池内高相关 → 组合实际分散度低，AI 选哪只都近似；但**扩大池子**的样本对齐/AI 注意力稀释/评测噪声/过拟合风险都很高。故第一步不是加标的，而是**先量化相关性**，用数据决定。
  - 改法：① 新增 `scripts/corr_analysis.py`：对配置池任意两标的计算 20 日收益滚动相关性，输出矩阵，阈值 ≥0.7 判定"冗余"；② 若确需扩充：优先低相关且数据完整（511010 国债ETF、518880 黄金ETF；需核验 akshare 可用性与复权），保留 `max_buy_count` 并加提示词约束"同类资产最多 1 个"；③ 提示词给每标的加资产类别标签。
  - 实验设计：池 A=现 3 宽基 vs 池 B=+低相关标的，同引擎同区间；另做"池不变仅改分散约束"的纯提示词对照（隔离混淆变量）。判定：池 B 样本外 Sharpe ≥ 池 A+0.1 **或** max_drawdown 下降 ≥3pp 且收益不劣化；相关性分析先行。
  - 风险与副作用：池子选择 = 特征选择，极易过拟合（样本外确认必须）；引入非权益资产改变"AI 选股"评测语义；数据缺失/复权问题。
  - 验收：pytest 断言相关性计算函数正确；配置池可动态扩展且缺失标的不崩溃；提示词含资产类别标签。

---

## 落地记录

> 每落地一条，追加：编号、落地内容、对应测试、回测 A/B 结果、是否保留。

- **PP-1（2026-08-09，落地，保留）**：`Settings.fill_mode`（close/next_open）；`execute_decisions` 新增可选 `fill_prices`（成交价与决策参考价分离，真实盘默认不变）；`Backtester` 回放预取下一根 bar 开盘价成交；`compute_benchmark` 支持佣金同口径；CLI `--fill` / `--commission-mult`。真实回测（rule 引擎，2025-06~2026-08，510300）：close +26.84% vs next_open +25.98% vs next_open+佣金×2 +25.64%，三口径方向一致（结论稳健），确认 close 假设乐观上界约 0.86pp。测试：`test_v05.py` 3 条 + `test_execute_decisions_default_fill_is_close`。
- **PP-3（2026-08-09，落地，保留）**：system 三段式（目标/决策框架/输出契约，`_system_prompt`）；`Settings.temperature`/`system_prompt_extra` + CLI `--temperature`/`--model` 透传；**缓存键修正** `md5(model|temperature|system|prompt)`——修掉"改 system 误用旧缓存"隐患。注意：改 system 后 AI 缓存整体失效，下次 AI 引擎回测会重新计费一次（一次性成本，已确认可接受）。测试：`test_v05.py` 5 条（system 三段式/温度透传/缓存键隔离/ai 与 ai_policy 共享保持）。
- **PP-2 数据层（2026-08-09，落地，保留）**：新增 `aitrader/features.py` 纯函数（ma5/10/20、ret_1d/5d/20d、vol_20d、rsi14、pct_from_high/low20、volume_ratio），各指标只依赖最近 N 根（局部性=无前视，有单测）。**prompt 注入已具备**（`--feature-inject` 开关 + `_format_features` 8 字段拼接，每标的 +30~45 token），**默认关**——配合 `--adjust hfq` 复权使用。测试：`test_features.py` 4 条 + `test_v07.py` 注入开关 2 条。
- **PP-4（2026-08-09，落地，保留）**：`Decision.confidence`（默认 0.5）；`RiskConfig.min_confidence_buy`（默认 0 关闭，向后兼容）；DeepSeek `_parse_item` 解析 confidence（缺失/非数→0.5）、`_validate` 越界标记 `invalid_confidence`、提示词加入 confidence 语义；`execute_decisions` 对 `confidence < min_confidence_buy` 拒绝并回填 `risk_rejected:low_confidence`（复用 P1-8 回填）；`decisions` 表新增 `confidence` 列（migration，buy 才存）；`rank_ic` Spearman 秩相关评测函数（纯 Python）；CLI `--min-confidence`。测试：`test_v06.py` 11 条。下一步：用 `--min-confidence {0.5,0.6,0.7}` 跑真实 AI 回测 A/B + Rank IC 校准度报告。
- **复权接入回测 + Rank IC 闭环（2026-08-09，落地，保留）**：`Backtester` 加 `adjust` 参数 + CLI `--adjust {none,hfq}`（benchmark 同口径）；`adjfactor.fetch_dividends` 加进程内缓存。**Rank IC 闭环**：`Backtester.run` 内存内收集有效 buy 的 confidence → `compute_forward_returns`（决策日收盘 → 决策日后 20 交易日收盘，无前视）→ `rank_ic`，返回 `rank_ic:{ic,n}` 并入回测输出，`run.py` 打印（n<15 标注"样本不足"）。真实验证：rule 引擎回测 2025-06~2026-08 输出 `Rank IC: +0.000 (n=26)`（rule 置信度恒 0.5→IC=0，管道正确）；复权后基准 +17.56%→+19.58%。测试：`test_v07.py` 5 条。下一步：AI 引擎跑 `--feature-inject --adjust hfq --min-confidence {0.5,0.6,0.7}` A/B（缓存键不含门槛 → 三档只计费一次全量 ≈240 次）。
- **P1-9 网络硬超时 + IPv6 修复（2026-08-09，落地，保留）**：本机无 IPv6 路由，新浪 DNS 返回前几条全是 IPv6，urllib3 逐条试 IPv6 失败后才轮到 IPv4 → 拉取从 0.5s 恶化到 60s（曾误判为"梯子/断网/锁死"）。修复：`datasource.py` 设 `socket.setdefaulttimeout(15)`（网络半死不再无限挂）+ monkeypatch `socket.getaddrinfo` 对 sina 域名过滤 IPv6 只留 IPv4（实测 60s→0.8s）。
- **PP-5~PP-8（已核验现状属实，排期）**：止损网格（PP-5）、历史盈亏反馈（PP-6）、政策归档回测（PP-7）、标的池相关性分散（PP-8）——未落地，按"地基先行"原则留待下一轮。
- **A-1 + A-4（2026-08-10，落地，保留，测试 128 全过）**：A-1 真实盘接入特征注入最优配置（config.json `feature_inject:true`，`--feature-inject` 批处理也生效）；A-4 `_date_type` 拒绝未来日期。⚠️ **但见 N-1：`batch._fetch_and_store` 未传 `adjust`，真实盘特征实为未复权价计算（A-1 只做了一半）**。
- **A-2/A-3/A-5 + P2-1/P2-2 + B-1/B-2 + B-3（2026-08-10，落地，保留，测试 136 全过）**：A-2 引擎级异常隔离（batch 引擎循环 try/except）；A-3 收盘后 15:00 运行守卫（盘中返回 `before_close`）；A-5 回测默认 end=最近已收盘交易日（`_last_closed_trading_day`）；P2-1 回测默认从 config 读（`fill_mode:next_open` + `adjust:hfq`，Settings 加 `adjust`）；P2-2 滑点建模（`RiskConfig.slippage_bps` + `execute_decisions` 买升卖降 + `--slippage`）；B-1 真实盘 Rank IC 校准闭环（`decisions.forward_return` 列 migration + `_calibrate_forward_returns` 回填 + 日报展示累计 rank_ic）；B-2 日报升级（今日决策明细 + 基准对照 + 数据截至日期，get_snapshots 返回 bar_date）；B-3 市场环境注入（`market_env_inject` 实现默认关，`--market-env` 仅回测生效，A/B 验证后开启——见 N-6）。
- **N-2 + N-12 + N-4（2026-08-10，落地，保留，测试 143 全过）**：N-2 崩溃遗留 running 不再卡死（`has_batch_run` 只认 done + `begin_batch_run` 同日 running 可重试，崩溃无快照自动补跑）；N-12 `daily_snapshots` 加 `source`（real/replay，历史回放不混入真实账本，含迁移）；N-4 日报新增"按信心分桶胜率"表（0~0.6 / 0.6~0.7 / 0.7+，独立于 IC 门槛有样本即渲染）。测试：`test_v12.py` 7 条 + v0.4 旧测试更新到新语义。N-1（真实盘特征复权，需双 bars）与 N-9（--catch-up）留待下一轮。
- **N-1（2026-08-11，落地，保留，测试 149 全过）**：真实盘特征复权——双 bars 方案。`batch._fetch_and_store` 返回三元组（原始 bars_map / 复权 adjusted_map / failed），`_adjusted_bars_map` 本地用 `compute_adjusted_bars` + 分红缓存生成复权（零额外网络，失败降级原始）；`DecisionContext.adjusted_bars` 字段；`DeepSeekEngine._feature_bars` 特征（feature_inject + market_env）优先复权、估值/成交/价格展示保持原始；`_calibrate_forward_returns` 改复权价回填（含分红，与回测 hfq 一致）。由此真实盘与回测"看到同一个世界"：除权日特征不再假跳变。测试：`test_v13.py` 6 条（特征优先级/回退/prompt 特征复权价+价格展示原始/batch 双 bars 传递/分红失败降级/fwd 含分红）。注：实现时发现 N-1 核心代码在上轮（v0.12 之后、未提交）已在工作区写好，本轮补测试、修 `_feature_bars` 空列表回退瑕疵、验收并提交为 v0.13。
- **N-3/N-5/N-6/N-8/N-9/N-10/N-11（2026-08-11，落地，保留，测试 161 全过）**：一次性收尾剩余工程项。N-3 `last_run.json` 加 `fill_note` 成交口径说明；N-5 新增 `aitrader/attribution.py`（reason 标签解析 + 按标签聚合已平仓配对盈亏）+ 提示词要求 [趋势/回调/政策/超买/超卖/其他] 标签 + 日报"归因复盘"表；N-6 `--market-env` 批处理接线 + config 暴露（默认关，未 A/B 保持关）；N-8 `DeepSeekEngine.api_calls/cache_hits` 记账 + `last_run.api_stats`；N-9 `--catch-up [N]` 补跑缺失交易日（幂等）；N-10 新增 `aitrader/notify.py`（check_alerts 失败/无成交/回撤 + send_notify 推送失败不阻塞）+ config.notify（默认关）；N-11 主源失败回退 `bars` 表缓存兜底（容灾，当日新鲜度仍硬校验，不造假；"缓存提速"评估风险 > 收益明确不做）。测试：`test_v14.py` 12 条。
- **🔥 v0.15 烧 API 实验（2026-08-11，测试 170 全过，已推送）**：用户授权烧 API，落地 PP-5/PP-6/PP-8 代码 + 跑 N-6/PP-6/PP-5 A/B。**免费**：PP-8 相关性（510300-588000=0.75、510300-159915=0.87、588000-159915=0.82，全>0.7 → 池内高度冗余）；PP-6 代码（feedback_n 默认0关 + 复盘节 + batch/backtest 注入）；PP-5 代码（apply_stop_rules + RiskConfig + 网格脚本）。**烧 API（AI 特征+复权 2025-06~2026-08，v0.14 改 system 后缓存全失效全量计费 ≈3×280 次）**：①基线 +13.40%/夏普0.98/回撤10.04%/4笔（注：比 v0.7 的 +21.70% 低，因 v0.14 改 system 加 reason 标签→模型更保守）；②N-6 +市场环境 +9.52%/夏普1.57/回撤2.86%/7笔 → **满足开启标准，config.json market_env_inject 已置 true**（更稳：回撤大减、夏普大幅提升；收益略降）；③PP-6 +反馈5笔 +10.34%/夏普1.45/回撤4.34%/11笔 → 满足提升标准但收益降更多+换手 1.75 偏高，**标注为可选项（feedback_n 默认仍 0）**。④PP-5 rule 网格 12 组合（2021-2024）：**无组合同时满足"夏普+0.1 且回撤-3pp"→ 保留无止损默认**（止盈 20% 改善收益但回撤无降）。**共同模式：加辅助信息→降绝对收益但改善风险调整表现；样本少（4-11笔）置信度有限，真实盘继续观察积累样本**。

---

## AI A/B 实验记录（2026-08-09，真实 DeepSeek 回测，2025-06-01~2026-08-01，next_open）

> 一次性实验成本 ≈1072 次 API 调用（数元人民币）。注意：prompt 含账户状态，门槛/特征改变执行→状态→未来 prompt→重新计费；因此各档是独立策略而非同序列过滤。
> ⚠️ **LLM 记忆局限（重要解读警示，2026-08-10 记）**：DeepSeek 是通用大模型，训练数据可能包含本回测区间（2025-06~2026-08）的市场信息——回测时模型可能"记得"这段历史（开卷考试），**回测绝对收益数字可能被高估，不可外推未来**。但 **A/B 相对对比仍有效**（各档受同样污染，相对优劣可信）。**真实盘（每天只有过去信息，模型无法预知明天）才是最终考场**。数据无前视已实测验证（end_date 过滤 + 逐日切片）。

| 配置 | 总收益 | 年化 | 回撤 | 夏普 | 胜率 | 盈亏比 | 成交 | Rank IC |
|---|---|---|---|---|---|---|---|---|
| 基线（无特征/无门槛） | +7.30% | 6.43% | 4.02% | 1.10 | 80% | 7.84 | 12 | n=11 不足 |
| 门槛 0.5 | +7.30% | 6.43% | 4.02% | 1.10 | 80% | 7.84 | 12 | n=11 不足 |
| 门槛 0.6 | +7.05% | 6.21% | 6.02% | 0.86 | 100% | 669 | 4 | n=9 不足 |
| 门槛 0.7 | +8.05% | 7.08% | 4.85% | 1.00 | 66.7% | 4.15 | 18 | +0.037 (n=225) |
| **特征+复权（两次一致）** | **+21.70%** | 18.97% | 7.47% | **1.77** | 77.8% | 24.13 | 20 | +0.029 (n=42) |

基准：原始 510300 买入持有 +17.56%；复权口径 +19.58%。

**结论（诚实）**：①AI 基线太保守（1 年 12 笔，跑输买入持有）——会选对（胜率 80%）但不敢动；②置信度门槛无系统性帮助，且 **Rank IC 全部≈0**（AI 的 confidence 无预测信息量）→ 门槛过滤的是无信息信号，不可依赖；③特征注入+复权方向性显著改善（+21.7% vs +7.3%，夏普 1.77 vs 1.10，跑赢基准），两次运行可复现，但归因有限（特征+复权整体效果，未拆分；AI 决策序列适配也可能贡献）；④confidence 校准无信息 → 不再投入优化该维度（除非换更强模型）；⑤下一步：考虑把"特征+复权"设为默认回测配置，真实盘谨慎开启验证。

## 大胆化 A/B（变体一「趋势跟随」，2026-08-09，已回滚）

免费诊断（回测落库 decisions）：hold 占 93%（679/732），buy 意向 44 但仅成交 20，sell 仅 9，confidence 全集中在 0.6~0.7 窄区间（无区分度→Rank IC≈0 的根因）→ 定位"过度保守"。

变体一（system 改为"趋势跟随·敢于上车"：MA20 双条件触发 + 点破空仓机会成本 + 量化追高底线 3%/RSI70，规则 5 同步改"趋势明确时敢于参与"）：

| 配置 | 总收益 | 夏普 | 回撤 | 成交 | Rank IC | vs 基准 |
|---|---|---|---|---|---|---|
| 对照组（原 system+特征+复权） | +21.70% | 1.77 | 7.47% | 20 | +0.029 (n=42) | 赢 |
| 变体一（趋势跟随） | +10.41% | 0.99 | 8.56% | 15 | **+0.210 (n=128)** | 输（+19.58%） |

**结论（诚实）**：变体一收益大跌（+21.7%→+10.4% 且跑输基准）——严格趋势触发条件 + 量化追高底线让 AI 更挑剔、错过行情 → **大胆化方向不成立，已回滚 system**。**意外发现**：变体一 Rank IC +0.210（n=128）远超对照组——**AI 的 confidence 校准能力是"可被提示词设计激活的"**，推翻了"confidence 无信息"的结论（之前无区分度可能是措辞问题非机制问题）。启示：confidence 有潜力，但需在不牺牲收益的前提下激活（未来可研究"分档建仓"变体二或温和措辞）。当前最优配置仍为：原 system + `--feature-inject --adjust hfq`。
