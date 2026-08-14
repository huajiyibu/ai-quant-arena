"""v0.24e 第五批：代理故障直连降级（体检03 P2-10，08-13 实锤代理拒绝全天剔除）。
retry_call 首次捕获 ProxyError → 对国内行情域名设置 NO_PROXY 白名单直连重试（不影响 DeepSeek API）。
"""
from pathlib import Path

p = Path("aitrader/util.py")
src = p.read_text(encoding="utf-8")

old = '''def retry_call(
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
'''
new = '''# 体检P2-10：国内行情域名 NO_PROXY 白名单（代理故障时降级直连；DeepSeek API 不受影响）
_PROXY_NO_PROXY = ".sina.com.cn,.sinajs.cn,.gtimg.cn,.eastmoney.com,.10jqka.com.cn"


def _is_proxy_error(exc: Exception) -> bool:
    name = type(exc).__name__
    if name == "ProxyError":
        return True
    text = f"{name}: {exc}".lower()
    return "proxy" in text or "10061" in text or "unable to connect to proxy" in text


def retry_call(
    fn: Callable[[], Any],
    retries: int = 3,
    base_delay: float = 1.0,
    label: str = "调用",
) -> Any:
    """执行 fn，失败重试 retries 次（指数退避 1s/2s/4s）；全部失败抛最后一次异常。

    体检P2-10：首次捕获代理故障 → 对国内行情域名降级直连（NO_PROXY 白名单），
    避免"开梯子/代理失效 → 行情全败 → 全天剔除"。
    """
    import os

    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt == 0 and _is_proxy_error(exc):
                os.environ["NO_PROXY"] = _PROXY_NO_PROXY
                logger.warning(
                    "%s 代理故障，已对国内行情域名降级直连（NO_PROXY 白名单）", label
                )
            logger.warning("%s失败（%d/%d）: %s", label, attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    assert last_exc is not None
    raise last_exc
'''
n = src.count(old)
assert n == 1, f"util: expected 1, got {n}"
src = src.replace(old, new)
p.write_text(src, encoding="utf-8")
print("util.py: 代理故障直连降级 done")
