"""数据库后端无关的持久化异常。

异常放在统一模块后，SQLite 与后续 MySQL 实现可以保持完全相同的业务语义。
"""


class SessionNotFoundError(LookupError):
    """找不到指定持久化会话。"""


class DraftNotFoundError(LookupError):
    """找不到指定行程草稿。"""


class VersionNotFoundError(LookupError):
    """找不到指定行程版本。"""


class DraftConflictError(RuntimeError):
    """草稿或候选版本发生并发修改冲突。"""


class TripTaskNotFoundError(LookupError):
    """找不到指定异步旅行规划任务。"""


class TaskIdempotencyConflictError(RuntimeError):
    """相同幂等键被用于不同请求。"""


class TaskLeaseLostError(RuntimeError):
    """Worker 已失去任务租约，不能继续写入进度或终态。"""


class UnsupportedDatabaseBackendError(ValueError):
    """配置了尚未注册的数据库后端。"""
