"""单实例文件锁：防止定时任务（15:30）与登录启动项并发触发导致重复成交/扣费。

原理：OS 文件锁（Windows msvcrt / Unix fcntl），拿不到锁的进程直接退出。
配合数据库层的同日幂等（has_snapshot）形成双层保险。
"""
from __future__ import annotations

from pathlib import Path


class FileLock:
    """基于 OS 文件锁的互斥锁"""

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self._fd = None

    def acquire(self) -> bool:
        """尝试获取锁；成功返回 True，已被其他进程占用返回 False"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.lock_path, "a+")
        # 确保文件至少 1 字节（Windows 锁定需要长度）
        if self._fd.tell() == 0:
            self._fd.write(" ")
            self._fd.flush()
        self._fd.seek(0)

        try:
            import msvcrt  # Windows
        except ImportError:
            import fcntl  # Linux / macOS

            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._fd.close()
                self._fd = None
                return False
            return True

        try:
            msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self._fd.close()
            self._fd = None
            return False
        return True

    def release(self) -> None:
        """释放锁（进程异常退出时由 finally 保证调用）"""
        if self._fd is None:
            return
        try:
            import msvcrt  # Windows

            self._fd.seek(0)
            msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl  # Linux / macOS

            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        self._fd.close()
        self._fd = None
