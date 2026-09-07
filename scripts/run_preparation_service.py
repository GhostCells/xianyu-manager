"""Preparation-only launcher with bounded, content-free operational logging."""

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys

from xianyu_manager.runtime_policy import PROCESS_POLICY


def exit_status(code, stopping):
    # A requested systemd stop can leave the child reporting its handled signal.
    # Do not translate -SIGTERM into exit 241 and mislabel a clean shutdown.
    return 0 if stopping and code in (0, -signal.SIGTERM, -signal.SIGINT) else code


def listen_arguments():
    uds = os.environ.get("XIANYU_MANAGER_API_UDS", "")
    if not uds:
        return ["--host", "127.0.0.1", "--port", "8765"]
    if uds != "/run/xianyu-runtime/api.sock":
        raise ValueError("PREPARATION_UDS_PATH_NOT_ALLOWED")
    return ["--uds", uds]


def validate_policy(policy, *, reply_only=False):
    # Starting the API never starts a browser. Login additionally requires a
    # fresh root manual-window capability in RuntimePolicy at every operation.
    if reply_only:
        if (policy.mode != 'normal' or not policy.reply_only or policy.account_id is None
                or policy.fulfillment_enabled or policy.order_recovery_enabled):
            raise SystemExit('REPLY_ONLY_LAUNCHER_REQUIRES_EXPLICIT_REPLY_ONLY_POLICY')
        return
    if policy.mode != 'prepare' or (policy.login_authorized and policy.account_id is None):
        raise SystemExit('PREPARATION_LAUNCHER_REQUIRES_PREPARE_AND_EXPLICIT_ACCOUNT')


def main(*, reply_only=False):
    validate_policy(PROCESS_POLICY, reply_only=reply_only)
    if not reply_only and os.environ.get("SILICONFLOW_API_KEY"):
        raise SystemExit("PREPARATION_MUST_NOT_HAVE_LLM_SECRET")
    log = logging.getLogger("preparation")
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        Path(os.environ["XIANYU_MANAGER_DATA_DIR"]).parent / "service.log",
        maxBytes=1024 * 1024,
        backupCount=3,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "xianyu_manager.app:app",
            *listen_arguments(),
            "--workers",
            "1",
            "--lifespan",
            "on",
            "--no-access-log",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("prepare_process_started")
    allowed = (
        "Application startup complete",
        "Application shutdown complete",
        "Waiting for application shutdown",
        "Shutting down",
    )
    suppressed = False
    for line in child.stdout:
        marker = next((x for x in allowed if x in line), None)
        if marker:
            log.info(marker)
        elif not suppressed:
            log.info("runtime_output_redacted")
            suppressed = True
    code = child.wait()
    log.info("prepare_process_exited code=%s", code)
    return exit_status(code, stopping)


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ['--reply-only']):
        raise SystemExit('UNSUPPORTED_LAUNCH_MODE')
    raise SystemExit(main(reply_only=sys.argv[1:] == ['--reply-only']))
