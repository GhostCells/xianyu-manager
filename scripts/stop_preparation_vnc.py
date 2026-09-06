"""Wait for this unit's VNC process to exit after its local stop acknowledgement."""

import os
from pathlib import Path
import subprocess
import time


def process_finished(pid):
    try:
        # A zombie has exited and only awaits its parent's reaping.
        return (
            (Path("/proc") / str(pid) / "stat")
            .read_text()
            .split(") ", 1)[1]
            .startswith("Z")
        )
    except FileNotFoundError:
        return True


def main():
    pid = int(os.environ["MAINPID"])
    if pid <= 1 or os.environ.get("DISPLAY") != ":99":
        raise SystemExit("INVALID_PREPARATION_VNC_STOP_TARGET")
    comm = Path("/proc") / str(pid) / "comm"
    if not comm.exists():
        return
    if comm.read_text().strip() != "x11vnc":
        raise SystemExit("INVALID_PREPARATION_VNC_STOP_TARGET")
    subprocess.run(
        ["/usr/bin/x11vnc", "-display", ":99", "-sync", "-R", "stop"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    # x11vnc's ACK precedes process exit. Returning on ACK alone makes systemd
    # immediately send SIGTERM, which x11vnc translates to failure status 2.
    for _ in range(100):
        if process_finished(pid):
            return
        time.sleep(0.05)
    raise SystemExit("PREPARATION_VNC_STOP_TIMEOUT")


if __name__ == "__main__":
    main()
