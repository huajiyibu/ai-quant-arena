"""真实盘每日健康检查：最新快照时点、今日决策、AI prompt 配置生效、成交留痕。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "aitrader.db"


def main() -> None:
    conn = sqlite3.connect(DB)
    print(f"=== 数据库: {DB}  (现在 {datetime.now():%Y-%m-%d %H:%M}) ===\n")

    # 1. 各账户最新快照（时点 vs 账本日期）
    print("--- 各账户最新快照 ---")
    rows = conn.execute(
        """SELECT a.engine_type, s.date, s.bar_date, s.source, s.total_assets, s.pnl
           FROM daily_snapshots s JOIN accounts a ON a.id = s.account_id
           WHERE s.date = (SELECT MAX(date) FROM daily_snapshots WHERE account_id = s.account_id)
           ORDER BY a.engine_type"""
    ).fetchall()
    for r in rows:
        stale = " ⚠️ bar_date≠date(用旧价)" if r[2] and r[2] != r[1] else ""
        print(
            f"  {r[0]:>9} | 账本日 {r[1]} | 行情日 {r[2]} | source={r[3]} | "
            f"总资产 {r[4]:,.2f} | 盈亏 {r[5]:+,.2f}{stale}"
        )

    # 2. 最近 3 天快照数（确认每天有快照，无跳日）
    print("\n--- 近 3 天快照（按账户） ---")
    for r in conn.execute(
        """SELECT a.engine_type, s.date FROM daily_snapshots s JOIN accounts a ON a.id=s.account_id
           WHERE s.date >= date('now','-4 day') ORDER BY a.engine_type, s.date"""
    ).fetchall():
        print(f"  {r[0]:>9} | {r[1]}")

    # 3. 今日决策
    print("\n--- 今日决策 (2026-08-11) ---")
    decs = conn.execute(
        """SELECT engine_type, symbol, action, amount, confidence, reason, fallback,
                  validation, execution_result
           FROM decisions WHERE date='2026-08-11' ORDER BY engine_type"""
    ).fetchall()
    if not decs:
        print("  (无决策记录)")
    for d in decs:
        print(
            f"  {d[0]:>9} | {d[1]:>6} {d[2]:>4} | amt={d[3]} conf={d[4]} | "
            f"{str(d[5])[:40]} | fb={d[6]} val={d[7]} exec={d[8]}"
        )

    # 4. AI prompt 配置生效检查（market_env / 特征 / 政策）
    print("\n--- AI prompt 配置生效 ---")
    for et in ("ai", "ai_policy"):
        row = conn.execute(
            "SELECT prompt_json FROM decisions WHERE date='2026-08-11' AND engine_type=? "
            "AND prompt_json IS NOT NULL LIMIT 1",
            (et,),
        ).fetchone()
        if not row:
            print(f"  {et}: 无 prompt（今天没决策？）")
            continue
        prompt = row[0] or ""  # prompt_json 列存的是 prompt 原文（非 JSON）
        print(
            f"  {et}: 市场环境={'✓' if '市场(' in prompt else '✗未注入'} | "
            f"特征={'✓' if '特征:' in prompt else '✗'} | "
            f"政策={'✓' if '宏观政策' in prompt else '✗'}"
        )
        # 抓一行市场环境样例
        for line in prompt.splitlines():
            if line.startswith("市场("):
                print(f"      样例: {line[:80]}")
                break

    # 5. 最近成交（确认 0 成交是否正常）
    print("\n--- 最近 5 笔成交（跨账户） ---")
    for r in conn.execute(
        """SELECT a.engine_type, t.date, t.symbol, t.action, t.price, t.volume, t.reason
           FROM trades t JOIN accounts a ON a.id = t.account_id
           ORDER BY t.date DESC, t.id DESC LIMIT 5"""
    ).fetchall():
        print(f"  {r[0]:>9} | {r[1]} | {r[2]} {r[3]} @{r[4]} x{r[5]} | {str(r[6])[:30]}")

    conn.close()


if __name__ == "__main__":
    main()
