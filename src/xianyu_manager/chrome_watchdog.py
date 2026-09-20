"""Independent, loopback-only browser liveness checks; no account data is read."""
import http.client
import json
import os
import sys
import threading


def probe_browser():
    connection = http.client.HTTPConnection("127.0.0.1", 9222, timeout=3)
    try:
        for path in ("/json/version", "/json/list"):
            connection.request("GET", path)
            response = connection.getresponse()
            if response.status != 200:
                return False
            data = json.loads(response.read(1024 * 1024))
            if path.endswith("version"):
                if not isinstance(data, dict) or not data.get("webSocketDebuggerUrl"):
                    return False
            elif not isinstance(data, list) or not any(
                item.get("type") == "page" for item in data if isinstance(item, dict)
            ):
                return False
        return True
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def watch_browser(crashed, *, probe=probe_browser, terminate=os._exit,
                  interval=10, threshold=3, shutdown=None):
    """Three consecutive failures tolerate brief stalls; page crashes fail promptly.

    No external requests, leases, credentials, or business switches are changed.
    systemd KillMode=control-group cleans all children before restarting the owner.
    """
    shutdown = shutdown if shutdown is not None else threading.Event()
    failures = 0
    while not shutdown.wait(interval):
        if crashed.is_set():
            reason = "PAGE_CRASH"
        else:
            failures = 0 if probe() else failures + 1
            if failures < threshold:
                continue
            reason = "CDP_UNHEALTHY"
        print("CHROME_WATCHDOG_FAILED:" + reason, file=sys.stderr, flush=True)
        terminate(1)
        return
