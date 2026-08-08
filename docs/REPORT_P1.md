# P1 功能报告 · walk-forward 回测 / 基准对比 / 科学指标

| 项目 | 内容 |
|---|---|
| 日期 | 2026-08-08 |
| 范围 | ai_trader v1.2（三引擎版） |
| 对应 | SRS v1.2 · DESIGN v1.2 |
| 测试 | 48 个 pytest 用例全过（原 39 + 新增 9） |

## 背景

此前系统只有"每天往后滚"的实时仿真，AI 引擎的真实水平要跑几个月才能看出来，且缺少基准与科学指标，无法判断"赚的是市场涨（beta）还是策略选得好（alpha）"。本次补齐回测评估闭环。

## 新增能力

### 1. walk-forward 回测（`--backtest`）

```bash
python run.py --backtest --start 2026-05-01 --end 2026-08-01   # 全部引擎
python run.py --backtest --engine rule --start ... --end ...    # 指定引擎
python run.py --backtest --benchmark 510300                     # 指定基准
```

- 新增 `aitrader/backtest.py`：`Backtester` 用历史行情**逐日回放**引擎决策，逐日只喂"截至当日"的行情（复用 P0 的 `end_date` 取数，无前视偏差）。
- 使用**独立数据库**（`data/backtest.db`），不污染每日仿真账本；每次回测前 `reset_account` 从初始资金重新开始，保证结果可重复。
- 回测区间内不注入当下宏观政策，保证评价一致性。

### 2. 科学指标（`compute_metrics`，纯函数）

总收益 / 年化收益（按 252 交易日折算）/ 最大回撤 / 夏普（日收益 × √252）/ 胜率与盈亏比（按 symbol 配对 buy→sell 计算已实现盈亏）/ 换手率（双边口径：买卖成交额合计 ÷ 平均总资产）。

### 3. 基准对比（`compute_benchmark`，纯函数）

以区间首日收盘价全额买入持有，逐日按收盘价折算资产，作为灰色虚线基准；与各引擎净值对比，归因 alpha/beta。

### 4. AI 响应缓存（防重复计费）

- `DeepSeekEngine` 新增 `response_cache`，按 `(model, prompt)` 的 MD5 缓存原始响应（prompt 已完整编码决策输入；ai 与 ai_policy 提示词相同时共享缓存）。
- CLI 回测时加载/持久化到 `data/ai_response_cache.json`，**重复回测同一区间不重复调用 API、不重复计费**。

### 5. 回测报表

- `reports/backtest_compare.png`：净值曲线对比（起点=1.0，顺带修复了日报里 1e6 轴难看的问题）+ 回撤面板 + 基准线。
- `reports/backtest_report.html`：每引擎指标表格 + 净值/回撤图。

## 验证

- 单测 9 个新增：指标计算（含空数据/亏损分支）、基准买入持有、回测回放产生成交、回测重置不累计、无行情降级、AI 响应缓存命中。
- 冒烟（真实数据 2026-05-01~08-07，规则引擎）：

```
[rule] 总收益 -3.25% | 年化 -11.70% | 最大回撤 6.40% | 夏普 -0.75 | 胜率 28.6% | 盈亏比 0.23 | 换手 2.96 | 成交 14 笔
基准(510300 买入持有): -2.80%
```

规则引擎在样本区间跑输基准，再次印证"写脚本容易、赚钱难"。

## 变更文件

| 文件 | 变更 |
|---|---|
| `aitrader/backtest.py` | 新增：`Backtester` / `compute_metrics` / `compute_benchmark` |
| `aitrader/reporter.py` | 新增：`plot_backtest_curves` / `build_backtest_report` |
| `aitrader/engines/deepseek.py` | 新增 `response_cache`（按 prompt 哈希缓存） |
| `aitrader/database.py` | 新增 `reset_account`（回测重置） |
| `run.py` | 新增 `--backtest` / `--start` / `--end` / `--benchmark` / `--record-decisions` |
| `tests/test_backtest.py` | 新增 9 个用例 |

## 说明与限制

- 回测未考虑滑点与涨跌停（仅手续费），与每日仿真口径一致。
- 回测取数窗口按区间动态计算（上限约 5000 根），支持 2.5 年以上长区间；换手率为双边口径（买卖成交额合计 ÷ 平均总资产）。
- AI 引擎回测需逐日调用 API（每次回测约 区间交易日 × 引擎数 次调用），响应缓存只对**相同区间/相同持仓状态**有效（prompt 含持仓，状态变化会重新调用）。
- `data/backtest.db` 按引擎保留最近一次回测结果；不同区间回测会覆盖同引擎的历史回测记录（每次重置）。
- **回测默认跳过 `AI·政策版`**：回测无历史政策源，政策版会退化为纯价格版（提示词与 `ai` 引擎完全一致），跑它会产生两条重复曲线误导判断；仅 `--engine ai_policy` 显式指定时才回测并给出提示。

## 遗留（未在本轮处理）

- P2：日志落盘、AI 调用重试、JSON 容错、止损/最小持有期等。
