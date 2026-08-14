"""v0.23d 第四批修复：
1. notify.py: send_notify 失败留档本地(data/notify_fail.log) + 记录失败原因（不再静默）
2. install_task.ps1: 计划任务 schtasks 也接 --catch-up 5（错过即补，消除与 .lnk 部署漂移）
"""
from pathlib import Path

# ---- notify.py ----
p = Path("aitrader/notify.py")
src = p.read_text(encoding="utf-8")
old = '''def send_notify(text: str, webhook_url: str) -> bool:
    """向 webhook 推送告警；任何失败仅记日志并返回 False（不阻塞主流程）。

    Server酱风格（GET /title/desp）与通用 POST JSON 都尝试。
    """
    if not webhook_url:
        return False
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
        except Exception:
            pass
        # 2) Server酱 GET（title/desp query）
        try:
            r = requests.get(
                webhook_url,
                params={"title": "AI Quant Arena 告警", "desp": text},
                timeout=10,
            )
            if r.ok:
                return True
        except Exception:
            pass
        return False
    except Exception:
        logger.exception("通知推送失败（不阻塞主流程）")
        return False
'''
new = '''def send_notify(text: str, webhook_url: str) -> bool:
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
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {reason} | {text[:120]}\\n")
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
'''
n = src.count(old)
assert n == 1, f"notify: expected 1, got {n}"
src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("notify.py: send_notify 失败留档 done")

# ---- install_task.ps1 ----
p = Path("scripts/install_task.ps1")
src = p.read_text(encoding="utf-8")
old = 'schtasks /Create /TN $taskName /TR \'"C:\\veighna_studio\\pythonw.exe" "D:\\下载的堆砌\\vnpy-4.4.0\\ai_demo\\ai_trader\\run.py"\' /SC DAILY /ST 15:30 /F'
new = 'schtasks /Create /TN $taskName /TR \'"C:\\veighna_studio\\pythonw.exe" "D:\\下载的堆砌\\vnpy-4.4.0\\ai_demo\\ai_trader\\run.py" --catch-up 5\' /SC DAILY /ST 15:30 /F'
n = src.count(old)
assert n == 1, f"install_task: expected 1, got {n}"
src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("install_task.ps1: 计划任务接 --catch-up 5 done")
print("ALL OK")
