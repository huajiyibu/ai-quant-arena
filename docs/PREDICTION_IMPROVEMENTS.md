# PREDICTION_IMPROVEMENTS — 预测/决策质量改进清单

> 这是「预测性能」专题的评审输出文档（工程可靠性清单见 `docs/IMPROVEMENTS.md`，二者分工，避免重复）。
> 由评审 AI 按 `docs/AI_REVIEW_PROMPT.md` 模板写入；主 agent 交叉验证后落地并更新验收状态。
> 铁律：**所有"变好了"的结论必须来自真实回测 A/B，禁止凭感觉、禁止虚构收益。**

## 评审轮次记录

| 轮次 | 日期 | 评审 AI | 范围 | 提交条数 | 主 agent 落地 |
|---|---|---|---|---|---|
| 1 | 2026-08-09 | 资深量化研究员评审 | 预测性能首次评审：输入信号 / 决策策略 / 模型与提示词 / 评估方法（工程可靠性项不重复，见 IMPROVEMENTS.md） | 8（PP-1~PP-8） | PP-1/PP-3 落地；PP-2 数据层落地（prompt 注入待复权）；PP-4~8 已核验、排期 |

---

## 待落地清单（按 P0 / P1 / P2 分级）

### P0（低成本高置信，直接落地）

- [x] **PP-1｜成交假设与手续费敏感度——先校准"尺子"再谈"变好"（【高置信】）**（2026-08-09 已落地，见落地记录）
  - 现状：`backtest.py::Backtester.run` 用当日收盘价成交（`prices={sym:bars[-1].close}` → `execute_decisions(..., prices, ...)`，`portfolio.py::execute_decisions` 里 `trade.price=price`）；`compute_benchmark` 基准买入持有**无手续费**；`compute_metrics` 无手续费档位；`RiskConfig.commission_rate=0.00025` 固定。
  - 问题：15:30 收盘后决策、按当日收盘价成交是**乐观上界**——次日开盘常跳空，乐观假设下"变好"的结论换到次日开盘可能消失；佣金虽低（双边 0.05%），高频换手（本系统换手已可见）下年化成本可观，当前把"毛利"当"净值"，无法区分真 alpha 与佣金税前的假收益。这是所有 A/B 判定可信度的地基，必须先校准。
  - 改法：① `config.py::Settings` 加 `fill_mode: str = "close"`（`"close"|"next_open"`）；② `Backtester.run` 回放循环预取下一根 bar，决策日 T 用 T+1 开盘价成交（T 日 15:30 决策时 T+1 开盘未知但为"下一交易日可执行价"，无前视）；③ `execute_decisions` 增加 `fill_price` 映射，把"决策参考价"与"成交价"分离；④ `run.py` 回测 CLI 加 `--fill` 与 `--commission-mult {0.5,1,2}`（重跑时传不同 `commission_rate`，AI 命中缓存不重复计费）；⑤ 基准 `compute_benchmark` 同口径扣佣金或报表标注"基准未计成本"。
  - 实验设计：同引擎同区间，A=close / B=next_open，佣金三档 × 两假设共 6 组输出指标表。判定"真的变好"必须**同时满足**：A、B 两假设下变体相对基线的改善方向一致（都为正才有效），且佣金 ×2 档下结论不反转；只在单侧成立的改善标记为"乐观假设伪提升"并降级。
  - 风险与副作用：回放循环改造（保留 next bar）改动面中等；可能大面积暴露"伪提升"，但这是评测价值本身；不改 schema、不动留痕体系。
  - 验收：pytest 断言 `fill_mode="next_open"` 时首笔成交价 == 次一交易日 `bar.open`；`commission_mult=2` 输出与 ×1 不同的指标；`conclusion_stable(close_A, close_B, open_A, open_B)` 一致性判定函数单测。

- [x] **PP-2｜技术指标特征注入——用确定性特征替代模型"心算"（【高置信】）**（数据层 `aitrader/features.py` 已落地；prompt 注入待复权数据接入后开启，见落地记录）
  - 现状：`deepseek.py::_build_prompt` 只喂 `closes=" ".join(f"{b.close:.3f}"...)`（最近 `lookback=20` 根裸收盘价），无 MA/RSI/波动率/量比/距高低点。
  - 问题：LLM 对 20 个数字串"心算"均线/动量/波动率精度不稳定（数字近因偏差、注意力分散），同一行情不同天可能输出漂移的趋势判断；二阶量（波动率）模型几乎无法可靠感知，止损/仓位没有依据；token 浪费在"重算数字"而非"决策"，幻觉式趋势描述挤占推理。喂确定性特征 = 代码精确计算、模型专注决策。
  - 改法：① 新增 `aitrader/features.py`：`compute_features(bars, lookback) -> dict` 纯函数，返回 `ma5/ma10/ma20`、`ret_5d/ret_20d`、`vol_20d`（日收益标准差）、`rsi14`、`pct_from_high20/pct_from_low20`、`volume_ratio`（5 日均量/20 日均量）；② `_build_prompt` 在每标的收盘价后追加一行 `特征: ma5=.. ma20=.. rsi14=.. ret_20d=+..% vol20=.. volume_ratio=..`（ETF 无可靠换手率数据，跳过换手）；③ **前置依赖**：未复权数据下除权日特征跳变失真，需先接前复权（见 IMPROVEMENTS P1-2）。
  - 实验设计：A=裸收盘价（现状）vs B=+特征，同 engine=`deepseek-chat`、同标的池、区间 2021-01~2024-12（含牛熊）。AI 缓存键含 prompt → 自动隔离，无串扰。判定：B 样本外 Sharpe ≥ A+0.1 且 max_drawdown 不劣化超 1pp，且 2021-2022、2023-2024 两个子区间方向一致；不满足则回退。
  - 风险与副作用：prompt 变长（每标的约 +150 token）；特征过多稀释注意力（控制在 8 个内）；依赖复权。改动小（纯新增 + prompt 一行）。
  - 验收：pytest 断言 `compute_features` 对已知 K 线输出精确值；`_build_prompt` 输出含 `rsi14`/`volume_ratio`；特征只用截至 `ctx.date` 的 bars 计算（无前视断言）。

- [x] **PP-3｜system prompt 结构化重写 + 决策契约 + 模型/温度实验开关（【高置信】）**（2026-08-09 已落地，见落地记录）
  - 现状：`deepseek.py::_call` 的 system 仅一句"你是一名谨慎的量化交易助手，风格保守，基于行情做波段。"；`temperature=0.3` 硬编码；`model=deepseek-chat` 来自 `config.py::Settings.model` 但 CLI 无法覆盖。
  - 问题：system 未定义"任务目标"（长期正期望、评测语境、宁缺毋滥）、未给"决策框架"（信号→风险→仓位三问）、未给"输出铁律"（不确定就 hold）。没有目标约束的"谨慎保守"是空话，模型倾向"永远轻微表态"而非"会说不做"。temperature/model 是零成本实验维度，当前被锁死，无法探索"更高确定性/更强推理"是否更赚。
  - 改法：① system 重写为三段：角色与目标（"长期正期望，不是每次都对；宁缺毋滥，信号不明确就 hold"）、决策框架（"先看趋势动量，再看波动率风险，最后定仓位；只对高置信信号动仓"）、输出契约（"只输出 JSON；buy 必须明确理由；不追高、不接飞刀、不用杠杆"）；② `Settings` 加 `temperature=0.3`、`system_prompt_extra=""`；`DeepSeekEngine.__init__` 加 `temperature` 参数；③ `run.py` 回测 CLI 加 `--temperature`/`--model` 覆盖（`build_engines` 透传）；④ **缓存键修正**：当前 `_call` 缓存键 `md5(model|prompt)` 不含 system/temperature，system 变更会误用旧缓存——改为把 system 摘要与 temperature 纳入缓存键。
  - 实验设计：A=现 system vs B=新 system（同 temperature=0.3）；B 再扫 temperature ∈ {0.2,0.5,0.8}、model ∈ {chat, reasoner}。判定：B 样本外 Sharpe ≥ A+0.1 且成交数 ≥20（避免"不敢交易"的假提升）；reasoner 成本 ×3~5 需单列成本列。
  - 风险与副作用：prompt 改动使全部 AI 缓存失效重算（一次性成本）；reasoner 延迟/成本高；system 过细可能引导过度保守。
  - 验收：pytest 断言 system 含"长期正期望"等三段关键词；`--temperature 0.7` 回测时请求体 `temperature==0.7`；缓存键含 system 摘要（两个不同 system 相同 prompt 不共享缓存）。

### P1（中等成本，需实验设计）

- [x] **PP-4｜结构化置信度 + 最小置信度买入门槛（【高置信】）**（2026-08-09 已落地，见落地记录）
  - 现状：`models.py::Decision` 无 confidence 字段；`deepseek.py::_parse_item` 只解析 action/amount/reason；`risk.py::validate_buy` 只按金额/持仓截断，无"模型自身信心"维度；`_validate` 无置信度校验。
  - 问题：模型被要求"谨慎"却无量化不确定性的出口，所有 buy 金额趋同，无法区分"强烈信号 vs 勉强信号"；amount 被模型当"态度"用，风控截断后实际仓位与模型意图脱节（评测归因失真）。显式 confidence 让"该不该动、动多重"可被评测与校准。
  - 改法：① `Decision` 加 `confidence: float = 0.5`；`_parse_item` 解析 `confidence`（0~1，缺省 0.5，越界置 0.5 并标记）；`_validate` 校验范围；② `RiskConfig` 加 `min_confidence_buy: float = 0.0`（默认关闭，向后兼容）；③ `portfolio.py::execute_decisions` 对 `buy` 且 `confidence < min_confidence_buy` → 跳过并回填 `execution_result="risk_rejected:low_confidence"`（复用已落地 P1-8 回填）；④ 提示词措辞："confidence 是你对这个信号带来正收益的信心（0~1），不是市场确定性；只对 confidence≥0.6 的信号考虑买入。"
  - 实验设计：A=现状（无门槛）vs B=min ∈ {0.5,0.6,0.7} 三档，同区间。指标：总收益/夏普/换手/成交数 + **校准度**（每次 buy 的 confidence 与后续 20 日实际收益做 Rank IC / 分档单调性）。判定：B 某档样本外 Sharpe ≥ A+0.1 且换手下降 ≥20%（过滤噪音交易）且 Rank IC 显著 >0（p<0.05）；若 IC≈0 说明 confidence 无信息量，如实报告并关闭门槛。
  - 风险与副作用：模型自我评估系统性虚高（校准差）；阈值过严致交易样本不足；confidence 解析需容错（字符串/缺失）。
  - 验收：pytest 断言含 confidence 的 JSON 正确解析、越界标记 invalid；`min_confidence_buy=0.6` 时 low-confidence buy 被拒并回填 execution_result；Rank IC 计算函数单测。

- [ ] **PP-5｜止损/止盈/回撤熔断网格——诚实检验"止损是否真的帮 AI"（【低置信探索】）**
  - 现状：`risk.py::validate_buy`、`portfolio.py::execute_decisions` 均无止损/止盈/回撤熔断；sell 完全由模型决定；`RiskConfig` 无相关参数（IMPROVEMENTS P1-5 已立项未落地）。
  - 问题：AI 卖出决策滞后于价格（只在决策日表态），单边下跌中可能死扛到深亏；**但止损对趋势型策略是双刃剑**——本场景 AI 无固定趋势风格，固定止损可能在正常回调中被迫离场、趋势恢复后再追高，形成"割肉+追高"双边损耗（震荡市更甚）。**故绝不默认止损有用，必须网格实测**；回撤熔断（账户级冷却）相对安全（防尾部风险、保护模拟盘本金语义）。
  - 改法：① `RiskConfig` 加 `stop_loss_pct=0.0`（0=关闭）、`take_profit_pct=0.0`、`max_drawdown_halt=0.0`、`halt_cooldown_days=5`；② 新增 `portfolio.py::apply_stop_rules(state, prices, risk, date) -> (state, forced_trades, halted)` 纯函数：持仓现价 ≤ 成本×(1-止损) → 强制整仓卖；≥ 成本×(1+止盈) → 止盈；账户回撤 ≥ 熔断 → 全部清仓 + 冷却标记（决策前由 `batch.py::_run_engine` 与 `backtest.py` 回放循环读冷却态，强制 hold N 日）；③ 在 `execute_decisions` 之前调用。
  - 实验设计：网格 stop_loss ∈ {0,5%,8%,12%} × take_profit ∈ {0,10%,20%}，对 rule 与 ai 引擎分别全组合回测同区间（2021-01~2024-12）；熔断 20%。判定（必须可回测）：某组合样本外 Sharpe ≥ 无止损基线 +0.1 **且** max_drawdown 下降 ≥3pp 才记为有效；**若所有止损档都不优于无止损，如实保留"无止损"为默认并记录结论**（这正是本次评审要诚实回答的问题）。多重比较防护：把 2025-01~2026-06 留作最终确认样本，网格只在训练段选优。
  - 风险与副作用：网格最优易过拟合（需留样本外）；止损+佣金在震荡市双杀；冷却期可能错过反弹。改动集中在 risk/portfolio 纯函数，schema 无变化。
  - 验收：pytest 断言触发止损生成强制 sell 成交且 reason 含 `[stop_loss]`；冷却期内引擎决策被替换为 hold；网格实验脚本输出全组合指标表并标注最优，供人审阅而非自动采纳。

- [ ] **PP-6｜历史交易盈亏反馈——让 AI 学会"复盘"（【低置信探索】）**
  - 现状：`deepseek.py::_build_prompt` 只含当前持仓成本与浮盈，无历史已平仓交易及盈亏；模型每次决策"失忆"，无法从过去买卖的错误中学习，重复追高/死扛模式无法自我纠正。
  - 问题：无行为反馈 = 模型在"无记忆的重复试错"。注入"近 N 笔已平仓交易 + 盈亏 + 当时理由"，让模型建立"行为→结果"关联（哪些理由导致亏损、哪些赚了），可抑制重复犯错；这是 LLM 决策特有的低边际成本增量（数据已在 trades 表）。
  - 改法：① `_build_prompt` 追加一节"近期已平仓交易（复盘参考）"：取近 `feedback_n`（配置，默认 5）笔**已平仓**（有 sell）的 (symbol, buy日期/价, sell日期/价, pnl_pct, buy_reason) 按时间倒序；② `DecisionContext` 增加 `recent_closed_trades: list[dict]`（由 `batch.py::_run_engine` / `backtest.py` 从各自库注入，回测用回测库 trades）；③ 缓存键自动含该节（prompt 变化）→ 不同历史不同 prompt，不串缓存；④ 铁律：只取**实际成交** trades，不含被风控拒绝/未成交的决策，避免"假亏损"污染复盘。
  - 实验设计：A=无反馈 vs B=近 5 笔 vs C=近 10 笔，同区间。判定：B/C 样本外 profit_factor ≥ A+0.1 且"重复亏损模式"（同标的高位追买再次亏损的连续次数）下降；控制近因偏差：反馈只含已发生（≤当日）交易，回测无前视。
  - 风险与副作用：反馈近因偏差（模型只学最近一段行情风格）；"过度反思"致成交数下降；prompt 变长。改动小（prompt 一节 + context 一字段）。
  - 验收：pytest 断言注入的反馈只含已平仓实际成交、不含未来成交（回测无前视）；条目 ≤ 配置上限；`feedback_n=0` 关闭。

- [ ] **PP-7｜政策时效与归因——先让"政策版"可回测（【低置信探索】）**
  - 现状：`batch.py::_fetch_policy` 拉政策但不落库；`datasource.py::AkSharePolicySource.fetch_macro_news` 返回 `"标题｜内容"` 整段（**未取财联社电报时间戳**），`_build_prompt` 整段塞入最多 8 条；`backtest.py` 中 `policy_text=""` → **ai_policy 永远无法在回测中验证**（IMPROVEMENTS P1-4 已提存档，此处聚焦其预测价值闭环）。
  - 问题：政策版与纯价格版孰优完全不可知（评测盲区）；"当日 15:30 后发布的政策"若塞进"当日 15:30 决策"即构成前视（用未来信息），当前无时间判断无法保证时效边界；无归因（政策→标的/板块映射缺失）导致模型无法判断"哪条政策影响哪个持仓"。
  - 改法：① 新增 `policy_archive` 表（date, time, title, content, hit_keywords），`_fetch_policy` 落库；`AkSharePolicySource` 增取时间戳列（需调研 `ak.stock_info_global_cls` 的列名，取不到用拉取日期兜底）；② `_build_prompt` 政策节改为结构化单行 `[09:35|央行|降准0.5pp|标题摘要]`，提示"判断是否已被价格反映（预期差），只对超预期的给行动；与你持仓无关则忽略"；③ `Backtester` 增加 `policy_by_date: dict[date, str]`（从 archive 按日期回放），决策日只注入 ≤ 当日 15:30 发布的历史政策（无前视）；否则维持 `policy_text=""` 并如实标注"政策版未验证"。
  - 实验设计：ai vs ai_policy（同日历史政策注入），区间取 archive 有数据后（先积累 3 个月再评估，或用手工标注样例区间）。判定：ai_policy 样本外 Sharpe ≥ ai+0.1 才认定政策有增量价值；若 <0.1 如实报告"政策无增量，可能为噪音"，并降级为不启用。
  - 风险与副作用：政策回放依赖 archive 数据积累（新功能，先落数据层）；政策时间与 15:30 决策的先后边界需严格校验（前视风险）；关键词过滤漏报/误报影响归因。
  - 验收：pytest 断言 archive 落库且含时间戳；回测注入的政策日期 ≤ 决策日 15:30；`policy_text` 结构化条目含时间/关键词/摘要。

### P2（探索性，可能过拟合，谨慎）

- [ ] **PP-8｜标的池分散与轮动——用相关性而非数量决定分散（【低置信探索】）**
  - 现状：`config.json::symbols` 仅 510300（沪深300ETF）/588000（科创50ETF）/159915（创业板ETF）三只宽基 ETF（科创50/创业板成长风格高度相关），`max_buy_count=2`；无轮动/择时切换；AI 只能在高度相关的池子里"伪选择"。
  - 问题：池内高相关 → 组合实际分散度低，AI 选哪只都近似（alpha 空间被相关结构锁死）；但**扩大池子的风险**：标的上市时间/数据缺失导致样本对齐问题、AI 注意力被稀释、评测噪声增大、且"选哪些标的进池"本身极易在样本外过拟合。故第一步不是加标的，而是**先量化相关性**，用数据决定。
  - 改法：① 新增 `scripts/corr_analysis.py`（或 tests 内函数）：对配置池任意两标的计算 20 日收益滚动相关性，输出矩阵，阈值 ≥0.7 判定"冗余"；② 若确需扩充：优先低相关且 ETF 数据完整（如 511010 国债ETF、518880 黄金ETF；**需核验 akshare 数据可用性与复权**），保留 `max_buy_count` 并加提示词约束"同类资产最多 1 个"；③ 提示词给每标的加资产类别标签（`510300(沪深300ETF/权益)`、`511010(国债ETF/利率)`）。
  - 实验设计：池 A=现 3 宽基 vs 池 B=+低相关标的（若数据可用），同引擎同区间；另做"池不变仅改分散约束"的纯提示词对照（隔离池子效应的混淆变量）。判定：池 B 样本外 Sharpe ≥ 池 A+0.1 **或** max_drawdown 下降 ≥3pp 且收益不劣化；相关性分析先行，若现池相关性全部 >0.85 而扩充池引入 <0.3 相关标的，组合夏普提升的**机械理由**才成立。
  - 风险与副作用：池子选择 = 特征选择，极易过拟合（样本外确认必须）；引入非权益资产改变"AI 选股"评测语义；数据缺失/复权问题。
  - 验收：pytest 断言相关性计算函数正确（已知序列）；配置池可动态扩展且缺失标的不崩溃；提示词含资产类别标签。

---

## 落地记录

> 每落地一条，追加：编号、落地内容、对应测试、回测 A/B 结果、是否保留。

- **PP-1（2026-08-09，落地，保留）**：`Settings.fill_mode`（close/next_open）；`execute_decisions` 新增可选 `fill_prices`（成交价与决策参考价分离，真实盘默认不变）；`Backtester` 回放预取下一根 bar 开盘价成交；`compute_benchmark` 支持佣金同口径；CLI `--fill` / `--commission-mult`。真实回测（rule 引擎，2025-06~2026-08，510300）：close +26.84% vs next_open +25.98% vs next_open+佣金×2 +25.64%，三口径方向一致（结论稳健），确认 close 假设乐观上界约 0.86pp。测试：`test_v05.py` 3 条 + `test_execute_decisions_default_fill_is_close`。
- **PP-3（2026-08-09，落地，保留）**：system 三段式（目标/决策框架/输出契约，`_system_prompt`）；`Settings.temperature`/`system_prompt_extra` + CLI `--temperature`/`--model` 透传；**缓存键修正** `md5(model|temperature|system|prompt)`——修掉"改 system 误用旧缓存"隐患。注意：改 system 后 AI 缓存整体失效，下次 AI 引擎回测会重新计费一次（一次性成本，已确认可接受）。测试：`test_v05.py` 5 条（system 三段式/温度透传/缓存键隔离/ai 与 ai_policy 共享保持）。
- **PP-2 数据层（2026-08-09，落地，保留）**：新增 `aitrader/features.py` 纯函数（ma5/10/20、ret_1d/5d/20d、vol_20d、rsi14、pct_from_high/low20、volume_ratio），各指标只依赖最近 N 根（局部性=无前视，有单测）。**prompt 注入未开启**：未复权数据下除权日特征跳变失真，待接入前复权（IMPROVEMENTS P1-2）后再注入。测试：`test_features.py` 4 条。
- **PP-4（2026-08-09，落地，保留）**：`Decision.confidence`（默认 0.5）；`RiskConfig.min_confidence_buy`（默认 0 关闭，向后兼容）；DeepSeek `_parse_item` 解析 confidence（缺失/非数→0.5）、`_validate` 越界标记 `invalid_confidence`、提示词加入 confidence 语义；`execute_decisions` 对 `confidence < min_confidence_buy` 拒绝并回填 `risk_rejected:low_confidence`（复用 P1-8 回填）；`decisions` 表新增 `confidence` 列（migration，buy 才存）；`rank_ic` Spearman 秩相关评测函数（纯 Python）；CLI `--min-confidence`。测试：`test_v06.py` 11 条。下一步：用 `--min-confidence {0.5,0.6,0.7}` 跑真实 AI 回测 A/B + Rank IC 校准度报告。
- **复权调研 + 数据层（2026-08-09，落地，保留）**：akshare 无现成 ETF 前复权接口（东财 `fund_etf_hist_em` 本机不可达、新浪 `fund_etf_hist_sina` 无 adjust 参数）；改用新浪 `fund_etf_dividend_sina`（日期+每份累计分红，510300 共 14 条）做**后复权式调整** `adjfactor.py`：`P_adj(t)=P(t)+截至t的累计分红`（只用截至当日信息→无前视；消除除权跳空）。`datasource.fetch_daily_bars` 增 `adjust="hfq"` 可选参数（默认关，失败降级原始行情）。真实验证：2025-06-18 除权日原始跳空 -0.085（误判下跌）→ 复权后连续无跳空。测试：`test_adjfactor.py` 4 条。**PP-2 前置复权已备**，特征注入可进入回测 A/B（下一步）。
- **PP-5~PP-8（已核验现状属实，排期）**：止损网格（PP-5）、历史盈亏反馈（PP-6）、政策归档回测（PP-7）、标的池相关性分散（PP-8）——未落地，按"地基先行"原则留待下一轮。
