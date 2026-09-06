"""Fixed per-process rehearsal policy; never persisted in business data."""
from dataclasses import dataclass
from functools import wraps
from inspect import iscoroutinefunction

from .config import read_safe_mode


class SafeModeOperationBlocked(RuntimeError):
    error_code = "SAFE_MODE_OPERATION_BLOCKED"


@dataclass(frozen=True)
class RuntimePolicy:
    safe_mode: bool = False

    def require_business(self) -> None:
        if self.safe_mode:
            raise SafeModeOperationBlocked(self.error_message)

    @property
    def error_message(self) -> str:
        return "SAFE_MODE_OPERATION_BLOCKED: 安全模式禁止业务执行；切换模式需要重启进程"


PROCESS_POLICY = RuntimePolicy(read_safe_mode())


def business_operation(function):
    """Reject before entering a method, including before DB/profile reads."""
    if iscoroutinefunction(function):
        @wraps(function)
        async def guarded(self, *args, **kwargs):
            self.runtime_policy.require_business()
            return await function(self, *args, **kwargs)
    else:
        @wraps(function)
        def guarded(self, *args, **kwargs):
            self.runtime_policy.require_business()
            return function(self, *args, **kwargs)
    return guarded
