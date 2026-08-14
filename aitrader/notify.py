"""异常/告警通知（N-10）：纯函数判定告警条件 + 可插拔推送，失败不阻塞主流程。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def check_alerts(
    ok: bool,
    error: str,
    snapshots: list[dict],
    trades_count: int,
    idle_days: int = 5,
    max_drawdown: float = 0.15,
) -> list[str]:
    """判定是否需要推送告警。返回告警文本列表（空 = 无需推送）。

    Args:
        ok: 本次批处理是否成功
        error: 失败原因（ok=False 时）
        snapshots: db.get_snapshots(account_id) 结果（含 total_assets）
        trades_count: 最近 idle_days 个交易日的成交笔数（0 = 疑似空转）
        idle_days: 无成交多少日触发提醒
        max_drawdown: 净值相对峰值回撤阈值（触发提醒）
    """
    alerts: list[str] = []
    if not ok:
        alerts.append(f"批处理失败: {error or '未知错误'}")
    if trades_count == 0:
        alerts.append(f"最近 {idle_days} 个交易日无成交（疑似数据/策略异常或长期空仓）")
    if snapshots:
        peak = max(s["total_assets"] for s in snapshots)
        last = snapshots[-1]["total_assets"]
        if peak > 0 and (peak - last) / peak >= max_drawdown:
            alerts.append(
                f"净值回撤达阈值: 峰值 {peak:,.0f} → 现值 {last:,.0f} "
                f"({(peak - last) / peak:.1%})"
            )
    return alerts


def send_notify(text: str, webhook_url: str) -> bool:
    """向 webhook 推送告警；失败记录原因到本地并返回 False（不阻塞主流程）。

    Server酱风格（GET /title/desp）与通用 POST JSON 都尝试。
    """
    if not webhook_url:
        return False

    def _log_fail(reason: str) -> None:
        # 体检P1-1：失败留档本地，无人值守下 webhook 挂掉也不静默
        try:
            from datetime import datetime
            from pathlib import Path

            fail_log = Path("data/notify_fail.log")
            fail_log.parent.mkdir(parents=True, exist_ok=True)
            with fail_log.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {reason} | {text[:120]}\n")
        except Exception:
            pass
        logger.warning("通知推送失败（不阻塞主流程）: %s", reason)

    try:
        import requests

        # 1) 通用 POST JSON
        try:
            r = requests.post(
                webhook_url,
                json={"title": "AI Quant Arena 告警", "text": text},
                timeout=10,
            )
            if r.ok:
                return True
            _log_fail(f"POST 非200(status={r.status_code})")
        except Exception as exc:
            _log_fail(f"POST 异常: {type(exc).__name__}: {exc}")
        # 2) Server酱 GET（title/desp query）
        try:
            r = requests.get(
                webhook_url,
                params={"title": "AI Quant Arena 告警", "desp": text},
                timeout=10,
            )
            if r.ok:
                return True
            _log_fail(f"GET 非200(status={r.status_code})")
        except Exception as exc:
            _log_fail(f"GET 异常: {type(exc).__name__}: {exc}")
        return False
    except Exception:
        logger.exception("通知推送失败（不阻塞主流程）")
        return False
