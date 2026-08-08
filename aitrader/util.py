"""通用工具：网络调用重试（指数退避）。

用于行情 / 政策 / AI 等偶发网络失败的可重试调用，降低"空仓日 / 数据缺失"累积。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def retry_call(
    fn: Callable[[], Any],
    retries: int = 3,
    base_delay: float = 1.0,
    label: str = "调用",
) -> Any:
    """执行 fn，失败重试 retries 次（指数退避 1s/2s/4s）；全部失败抛最后一次异常。"""
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            logger.warning("%s失败（%d/%d）: %s", label, attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc
