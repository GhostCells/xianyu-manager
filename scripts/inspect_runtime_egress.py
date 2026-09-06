"""Read-only observation. Never grants login, enforcement approval or sending."""

import json
import subprocess
import time


def command(*args):
    return subprocess.check_output(args, text=True, timeout=15).strip()


def inspect():
    result = dict(
        checked_at=time.time(),
        exit_node_ip=None,
        client_running=False,
        routes_ready=False,
        observed_public_ip=None,
        reviewed_public_ip=None,
        review_required=True,
        enforcement_verified=False,
        path_kind="unknown",
    )
    try:
        status = json.loads(command("tailscale", "status", "--json"))
        peers = [p for p in status.get("Peer", {}).values() if p.get("ExitNode")]
        peer = peers[0] if len(peers) == 1 else {}
        result["exit_node_ip"] = next(
            (ip for ip in peer.get("TailscaleIPs", []) if ":" not in ip), None
        )
        result["client_running"] = (
            status.get("BackendState") == "Running" and peer.get("Online") is True
        )
        result["routes_ready"] = all(
            "default dev tailscale0"
            in command("ip", flag, "route", "show", "table", "52")
            for flag in ["-4", "-6"]
        )
        result["path_kind"] = (
            "direct"
            if peer.get("CurAddr")
            else "relay" if peer.get("Relay") else "unknown"
        )
        result["observed_public_ip"] = command(
            "curl", "-4", "-sS", "--max-time", "10", "https://icanhazip.com"
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        result["observation_failed"] = True
    return result


if __name__ == "__main__":
    print(json.dumps(inspect()))
