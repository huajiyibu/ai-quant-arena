# P0 修复报告 · 回放前视 / 同日幂等 / 交易日判断

| 项目 | 内容 |
|---|---|
| 日期 | 2026-08-08 |
| 范围 | ai_trader v1.1（三引擎版） |
| 对应 | SRS v1.1 · DESIGN v1.1 |
| 测试 | 39 个 pytest 用例全过（原 35 + 新增 4） |

## 背景

评审时发现三个会影响评测数据可信度的 P0 问题，本次全部修复：

1. `--date` 回放存在前视偏差
2. 同日重复运行无幂等，可能重复成交
3. 无交易日判断，周末/节假日会制造虚假快照

---

## 问题 1：回放 `--date` 存在前视偏差

**现象**：`python run.py --date 2026-08-06` 回放时，喂给引擎的行情与成交价都来自"今天"的最新数据，却把快照盖在历史日期上，回放结果不可信。

**根因**：`DataSource.fetch_daily_bars(symbol, days, exchange)` 无日期参数，永远拉最新数据；`BatchRunner` 也未把回放日期传给数据源。

**修复**：
- `datasource.py`：`DataSource` / `AkShareDataSource` / `FakeDataSource` 的 `fetch_daily_bars` 增加 `end_date: datetime | None` 参数；`AkShareDataSource` 按 `df["date"] <= end_date` 过滤后再取最近 `days` 根。
- `batch.py`：`_fetch_and_store(end_date)` 把回放日期传入数据源。

**验证**：
- 单测 `test_replay_respects_end_date`：返回的最后一根 K 线即回放日，不含未来数据。
- 冒烟：`--date 2026-08-07` 回放正常生成该日快照。

---

## 问题 2：同日重复运行无幂等

**现象**：定时任务（15:30）+ 登录启动项兜底，若开机早于 15:30，同一天会跑两次，可能产生重复成交/重复决策，账本被污染。

**根因**：`trades` / `decisions` 表无去重约束，`add_trade` 无条件 INSERT；批处理未做"该日已处理"检查。

**修复**：
- `database.py`：新增 `has_snapshot(account_id, date)` 与 `get_snapshot(account_id, date)`。
- `batch.py`：`run(date, force=False)`；`_run_engine` 在决策前检查"该账户该日已有快照"则跳过（返回 `skipped=True`）。
- `run.py`：新增 `--force` 参数跳过幂等检查，强制重跑。

**验证**：
- 单测 `test_same_day_run_is_idempotent`：同日跑两次，成交/决策均只有 1 条。
- 单测 `test_force_reruns_same_day`：`force=True` 重跑会重新决策，但已持仓的重复买入被风控拒绝。
- 冒烟：`--date 2026-08-07` 连续跑两次，第二次提示"该日已处理过，跳过（--force 可强制重跑）"。

---

## 问题 3：无交易日判断

**现象**：周末/节假日照跑，akshare 返回的还是上一交易日 K 线，系统却以周六/周日的日期写入快照与决策，资金曲线凭空多出若干点。

**根因**：没有交易日历，任何日期都执行批处理。

**修复**：
- `datasource.py`：新增 `is_trading_day(date)`（协议方法）；`AkShareDataSource` 用 akshare 交易日历 `tool_trade_date_hist_sina()`（模块级缓存），获取失败时降级为仅跳过周末；`FakeDataSource` 支持注入 `trading_days` 集合（默认每天都是交易日）。
- `batch.py`：`run()` 开头判断非交易日直接返回，不产生账户/快照。

**验证**：
- 单测 `test_non_trading_day_skips_batch`：非交易日返回空结果、不建账户。
- 冒烟：`--date 2026-08-09` 输出"非交易日，跳过批处理"。

---

## 变更文件

| 文件 | 变更 |
|---|---|
| `aitrader/datasource.py` | `fetch_daily_bars` 加 `end_date`；新增 `is_trading_day` + 交易日历缓存 |
| `aitrader/database.py` | 新增 `has_snapshot` / `get_snapshot` |
| `aitrader/batch.py` | `run(force)`；交易日跳过；按日期取数；同日幂等 |
| `run.py` | 新增 `--force`；汇总输出区分"跳过/非交易日" |
| `tests/test_batch.py` | 新增 4 个用例 |

## 遗留（未在本轮处理）

- P1：walk-forward 回测、基准对比与科学指标、AI 自我反馈。
- P2：日志落盘、AI 调用重试、JSON 容错、止损/最小持有期等。
