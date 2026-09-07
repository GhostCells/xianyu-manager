"""Fixed per-process rehearsal policy; never persisted in business data."""

from dataclasses import dataclass, field
from functools import wraps
from inspect import iscoroutinefunction, signature
from pathlib import Path
import time

from .config import read_safe_mode, read_runtime_options
from .egress_control import read_root_json, clock_valid


class SafeModeOperationBlocked(RuntimeError):
    error_code = "SAFE_MODE_OPERATION_BLOCKED"


class RuntimeOperationBlocked(RuntimeError):
    error_code = "RUNTIME_OPERATION_BLOCKED"


@dataclass(frozen=True)
class RuntimePolicy:
    safe_mode: bool = False
    prepare_mode: bool = False
    account_id: int | None = None
    login_authorized: bool = False
    egress_status_path: Path | None = None
    _state: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def mode(self):
        return (
            "safe" if self.safe_mode else "prepare" if self.prepare_mode else "normal"
        )

    @property
    def managed(self):
        return self.prepare_mode or self.account_id is not None

    def require_account(self, account_id):
        if self.managed and (self.account_id is None or account_id != self.account_id):
            raise RuntimeOperationBlocked("RUNTIME_ACCOUNT_NOT_BOUND_OR_MISMATCH")

    def egress_status(self):
        result = {
            "ready": False,
            "reason": "EGRESS_UNKNOWN",
            "enforcement_verified": False,
        }
        if not self.managed:
            return {**result, "reason": "LEGACY_UNMANAGED"}
        try:
            path = self.egress_status_path
            if path is None or path.is_symlink() or path.stat().st_mode & 0o022:
                return result
            data = read_root_json(path)
            if not clock_valid(data):
                return {**result, 'reason': 'EGRESS_UNTRUSTED_OR_EXPIRED'}
            age = time.time() - float(data["checked_at"])
            ready = (
                0 <= age <= 60
                and data.get("exit_node_ip") == "100.66.224.40"
                and data.get("client_running") is True
                and data.get("routes_ready") is True
                and data.get("enforcement_verified") is True
                and data.get("review_required") is False
                and bool(data.get("observed_public_ip"))
                and data.get("observed_public_ip") == data.get("reviewed_public_ip")
            )
            return {
                "ready": ready and not self._state.get("egress_latched", False),
                "reason": (
                    "EGRESS_RESTART_REVIEW_REQUIRED"
                    if self._state.get("egress_latched")
                    else "EGRESS_READY" if ready else "EGRESS_UNVERIFIED_OR_STALE"
                ),
                "enforcement_verified": data.get("enforcement_verified") is True,
                "checked_at": data.get("checked_at"),
                "stale": not 0 <= age <= 60,
                "exit_node_ip": data.get("exit_node_ip"),
                "client_running": data.get("client_running") is True,
                "routes_ready": data.get("routes_ready") is True,
                "review_required": data.get("review_required") is not False,
                "path_kind": data.get("path_kind", "unknown"),
            }
        except (OSError, ValueError, TypeError, KeyError):
            return result

    def require_egress(self):
        if self.managed and not self.egress_status()["ready"]:
            self._state["egress_latched"] = True
            raise RuntimeOperationBlocked("EGRESS_BLOCKED_REVIEW_AND_RESTART_REQUIRED")

    def require_login(self, account_id):
        if self.safe_mode:
            raise SafeModeOperationBlocked(self.error_message)
        self.require_account(account_id)
        if self.managed and not self.login_authorized:
            raise RuntimeOperationBlocked("REAL_LOGIN_NOT_AUTHORIZED")
        self.require_egress()
        if self.prepare_mode:
            self.preparation_permission('login')

    def preparation_permission(self, action):
        """Only a fresh root-produced manual window permits preparation IO."""
        if self.mode != 'prepare' or not self.login_authorized:
            raise RuntimeOperationBlocked('PREPARATION_PERMISSION_REQUIRED')
        self.require_egress()
        try:
            data = read_root_json(self.egress_status_path)
        except (OSError, ValueError, TypeError):
            raise RuntimeOperationBlocked('PREPARATION_PERMISSION_REQUIRED') from None
        if (not clock_valid(data) or data.get('purpose') != 'manual_login_inventory'
                or data.get('account_id') != self.account_id
                or not isinstance(data.get('operations'), list)
                or action not in data.get('operations', [])
                or not data.get('approval_id')):
            raise RuntimeOperationBlocked('PREPARATION_PERMISSION_REQUIRED')
        return str(data['approval_id'])

    def snapshot(self):
        egress = self.egress_status()
        can_login = (
            not self.safe_mode
            and self.account_id is not None
            and self.login_authorized
            and egress["ready"]
        )
        if can_login and self.prepare_mode:
            try:
                self.preparation_permission('login')
            except (OSError, ValueError, RuntimeError):
                can_login = False
        return {
            "mode": self.mode,
            "safe_mode": self.safe_mode,
            "prepare_mode": self.mode == "prepare",
            "runtime_account_id": self.account_id,
            "login_allowed": can_login,
            "automation_allowed": self.mode == "normal"
            and (not self.managed or egress["ready"]),
            "egress": egress,
            "business_forbidden": self.mode != "normal",
            "data_purpose": (
                "preparation_not_production" if self.mode == "prepare" else self.mode
            ),
        }

    def require_business(self) -> None:
        if self.safe_mode:
            raise SafeModeOperationBlocked(self.error_message)
        if self.prepare_mode:
            raise RuntimeOperationBlocked("PREPARE_BUSINESS_FORBIDDEN")
        self.require_egress()

    @property
    def error_message(self) -> str:
        return "SAFE_MODE_OPERATION_BLOCKED: 安全模式禁止业务执行；切换模式需要重启进程"


PROCESS_POLICY = RuntimePolicy(read_safe_mode(), **read_runtime_options())


def business_operation(function):
    """Reject before entering a method, including before DB/profile reads."""
    if iscoroutinefunction(function):

        @wraps(function)
        async def guarded(self, *args, **kwargs):
            self.runtime_policy.require_business()
            bound = signature(function).bind_partial(self, *args, **kwargs)
            if "account_id" in bound.arguments:
                self.runtime_policy.require_account(bound.arguments["account_id"])
            return await function(self, *args, **kwargs)

    else:

        @wraps(function)
        def guarded(self, *args, **kwargs):
            self.runtime_policy.require_business()
            bound = signature(function).bind_partial(self, *args, **kwargs)
            if "account_id" in bound.arguments:
                self.runtime_policy.require_account(bound.arguments["account_id"])
            return function(self, *args, **kwargs)

    return guarded


def login_operation(function):
    def check(owner, args, kwargs):
        bound = signature(function).bind_partial(owner, *args, **kwargs)
        account = bound.arguments.get("account_id", getattr(owner, "_account_id", None))
        owner.runtime_policy.require_login(account)

    if iscoroutinefunction(function):

        @wraps(function)
        async def guarded(self, *args, **kwargs):
            check(self, args, kwargs)
            return await function(self, *args, **kwargs)

    else:

        @wraps(function)
        def guarded(self, *args, **kwargs):
            check(self, args, kwargs)
            return function(self, *args, **kwargs)

    return guarded
