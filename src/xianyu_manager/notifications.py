from __future__ import annotations

import os
import subprocess
import time


class WindowsNotifier:
    """Best-effort Windows balloon notifications with duplicate suppression."""

    def __init__(self, *, deduplicate_seconds: int = 300) -> None:
        self.deduplicate_seconds = max(30, deduplicate_seconds)
        self._last_sent: dict[str, float] = {}

    def notify(self, title: str, message: str) -> bool:
        if os.name != "nt":
            return False
        safe_title = " ".join(str(title).split())[:80] or "闲鱼管理系统"
        safe_message = " ".join(str(message).split())[:240]
        fingerprint = f"{safe_title}\n{safe_message}"
        now = time.monotonic()
        if now - self._last_sent.get(fingerprint, 0.0) < self.deduplicate_seconds:
            return False

        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.BalloonTipIcon=[System.Windows.Forms.ToolTipIcon]::Info;"
            "$n.BalloonTipTitle=$env:XIANYU_NOTICE_TITLE;"
            "$n.BalloonTipText=$env:XIANYU_NOTICE_MESSAGE;"
            "$n.Visible=$true;"
            "$n.ShowBalloonTip(8000);"
            "Start-Sleep -Seconds 9;"
            "$n.Dispose()"
        )
        environment = os.environ.copy()
        environment["XIANYU_NOTICE_TITLE"] = safe_title
        environment["XIANYU_NOTICE_MESSAGE"] = safe_message
        try:
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    script,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return False
        self._last_sent[fingerprint] = now
        return True
