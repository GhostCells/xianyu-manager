"""Actual display readiness, not merely systemd start ordering."""

import subprocess
import time

for _ in range(30):
    if (
        subprocess.run(
            ["xdpyinfo"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).returncode
        == 0
    ):
        break
    time.sleep(0.5)
else:
    raise SystemExit("PREPARATION_DISPLAY_NOT_READY")
