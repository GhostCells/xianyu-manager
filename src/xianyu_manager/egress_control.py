"""Root-owned producer for the existing RuntimePolicy contract. No import-time IO.

Deployment copies this standalone stdlib-only file to a root-owned directory.
Do not execute a mutable development checkout as a privileged monitoring service.
"""

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time

EXIT_NODE = "100.66.224.40"
NAMESPACE = "xianyu-business"
HOST_IF = "xmg-host"
CLIENT_IP = "10.203.0.2"
UNIT = "xianyu-isolated-prepare.service"
STATE = Path("/run/xianyu-egress/status.json")
APPROVAL = Path("/etc/xianyu-egress/approval.json")
LATCH = Path("/var/lib/xianyu-egress/blocked.json")
LEASE_SECONDS = 30
STATUS_SECONDS = 20


def trusted_path(path, *, regular=True):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("UNTRUSTED_STATE_PATH")
    for entry in [path, *path.parents]:
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("STATE_NOT_ROOT_CONTROLLED")
        if entry != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("UNTRUSTED_STATE_PARENT")
    if regular and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("STATE_NOT_REGULAR")


def read_root_json(path):
    trusted_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
            raise ValueError("UNTRUSTED_STATE_FILE")
        text = stream.read(65537)
    if len(text) > 65536:
        raise ValueError("STATE_TOO_LARGE")
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("INVALID_STATE_DOCUMENT")
    return result


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def clock_valid(data, *, wall=None, mono=None, boot=None):
    try:
        wall = time.time() if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        boot = boot_id() if boot is None else boot
        at, tick, until = (
            float(data[k])
            for k in ("checked_at", "checked_monotonic", "valid_until_monotonic")
        )
        return (
            data.get("schema_version") == 1
            and data.get("boot_id") == boot
            and all(math.isfinite(x) for x in (at, tick, until))
            and 0 <= wall - at <= STATUS_SECONDS
            and 0 <= mono - tick <= STATUS_SECONDS
            and tick < until <= tick + STATUS_SECONDS
            and mono < until
        )
    except (KeyError, ValueError, TypeError, OSError):
        return False


def atomic_root_json(path, data):
    trusted_path(path.parent, regular=False)
    if path.exists() or path.is_symlink():
        trusted_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o644)
            json.dump(data, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(*args, input=None):
    return subprocess.run(
        args,
        input=input,
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
    ).stdout.strip()


def rule_digest(document):
    """Ignore handles/counters and ONLY the expiring lease's dynamic contents."""

    def clean(value):
        if isinstance(value, list):
            return [clean(v) for v in value]
        if not isinstance(value, dict):
            return value
        omit = {"handle", "packets", "bytes"}
        if value.get("name") == "lease" and value.get("table") == "xianyu_guard":
            omit |= {"elem", "elements"}
        return {k: clean(v) for k, v in value.items() if k not in omit}

    entries = [x for x in document["nftables"] if "metainfo" not in x]
    return hashlib.sha256(
        json.dumps(clean(entries), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def approval_valid(approval, *, now, boot):
    try:
        address = ipaddress.IPv4Address(approval["reviewed_public_ip"])
        return (
            approval.get("approved") is True
            and approval.get("exit_node_ip") == EXIT_NODE
            and address.is_global
            and approval.get("boot_id") == boot
            and 0 < float(approval["expires_at"]) - now <= 86400
            and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", approval["approval_id"]))
            and all(
                re.fullmatch(r"[0-9a-f]{64}", approval[k])
                for k in ("rules_sha256", "fault_report_sha256")
            )
        )
    except (KeyError, ValueError, TypeError):
        return False


def evaluate(approval, observation, latched, *, now, boot):
    if not approval_valid(approval, now=now, boot=boot):
        return "APPROVAL_REQUIRED"
    if latched == approval["approval_id"]:
        return "EXPLICIT_REVIEW_REQUIRED"
    if observation.get("exit_node_ip") != EXIT_NODE:
        return "WRONG_EXIT_NODE"
    for field in (
        "client_running",
        "routes_ready",
        "namespace_verified",
        "runtime_verified",
        "no_flow_offload",
    ):
        if observation.get(field) is not True:
            return field.upper() + "_FAILED"
    if observation.get("rules_sha256") != approval["rules_sha256"]:
        return "RULESET_CHANGED"
    if observation.get("observed_public_ip") != approval["reviewed_public_ip"]:
        return "PUBLIC_ADDRESS_REVIEW_REQUIRED"
    return "EGRESS_READY"


def runtime_verified():
    import pwd  # Linux updater only; keep legacy Windows policy imports portable.

    props = dict(
        line.split("=", 1)
        for line in run(
            "systemctl", "show", UNIT, "-p", "MainPID", "-p", "ControlGroup"
        ).splitlines()
    )
    main = int(props["MainPID"])
    if main <= 1:
        return False
    ns = Path("/run/netns") / NAMESPACE
    inode = ns.stat().st_ino
    group = props["ControlGroup"]
    if not group.startswith("/system.slice/"):
        return False
    groupdir = Path("/sys/fs/cgroup" + group)
    pids = {p for f in groupdir.rglob("cgroup.procs") for p in f.read_text().split()}
    uid = pwd.getpwnam("xianyu-runtime").pw_uid
    if uid in (0, pwd.getpwnam("ubuntu").pw_uid) or str(main) not in pids:
        return False
    for pid in pids:
        proc = Path("/proc") / pid
        if proc.stat().st_uid != uid or (proc / "ns/net").stat().st_ino != inode:
            return False
    return True


def observe():
    ts = json.loads(run("tailscale", "status", "--json"))
    selected = [v for v in ts.get("Peer", {}).values() if v.get("ExitNode")]
    peer = selected[0] if len(selected) == 1 else {}
    route = json.loads(
        run(
            "ip",
            "-j",
            "-4",
            "route",
            "get",
            "1.1.1.1",
            "from",
            CLIENT_IP,
            "iif",
            HOST_IF,
        )
    )
    ns_routes = json.loads(
        run("ip", "-n", NAMESPACE, "-j", "-4", "route", "show", "default")
    )
    reply_route = json.loads(
        run("ip", "-j", "-4", "route", "get", CLIENT_IP, "iif", "tailscale0")
    )
    all_rules = json.loads(run("nft", "-j", "list", "ruleset"))
    guard = json.loads(run("nft", "-j", "list", "table", "inet", "xianyu_guard"))
    nat = json.loads(run("nft", "-j", "list", "table", "ip", "xianyu_nat"))
    scope = json.loads(run("nft", "-j", "list", "table", "ip", "xianyu_forward_scope"))
    combined = {"nftables": guard["nftables"] + nat["nftables"] + scope["nftables"]}
    return {
        "exit_node_ip": next(
            (x for x in peer.get("TailscaleIPs", []) if ":" not in x), None
        ),
        "client_running": ts.get("BackendState") == "Running"
        and peer.get("Online") is True,
        "routes_ready": len(route) == 1
        and Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == "1"
        and route[0].get("dev") == "tailscale0"
        and len(ns_routes) == 1
        and len(reply_route) == 1
        and reply_route[0].get("dev") == HOST_IF
        and ns_routes[0].get("gateway") == "10.203.0.1",
        "namespace_verified": Path("/run/netns/" + NAMESPACE).exists(),
        "runtime_verified": runtime_verified(),
        "no_flow_offload": '"flowtable"' not in json.dumps(all_rules)
        and '"flow"' not in json.dumps(all_rules),
        "rules_sha256": rule_digest(combined),
        # Force the authorized interface and ignore all shell proxy variables.
        # Actual namespace/stack behavior still requires the separate fault report.
        "observed_public_ip": run(
            "curl",
            "--noproxy",
            "*",
            "--interface",
            "tailscale0",
            "-4",
            "-fsS",
            "--max-time",
            "6",
            "https://icanhazip.com",
        ).strip(),
        "path_kind": (
            "direct"
            if peer.get("CurAddr")
            else "relay" if peer.get("Relay") else "unknown"
        ),
    }


def set_lease(allowed):
    batch = "flush set inet xianyu_guard lease\n"
    if allowed:
        batch += f"add element inet xianyu_guard lease {{ {CLIENT_IP} timeout {LEASE_SECONDS}s }}\n"
    run("nft", "-f", "-", input=batch)


def update_once():
    approval = {}
    observation = {}
    reason = "UPDATE_FAILED"
    started = time.monotonic()
    try:
        approval = read_root_json(APPROVAL)
        latched = read_root_json(LATCH).get("approval_id") if LATCH.exists() else None
        if not approval_valid(approval, now=time.time(), boot=boot_id()):
            reason = "APPROVAL_REQUIRED"
        elif latched == approval["approval_id"]:
            reason = "EXPLICIT_REVIEW_REQUIRED"
        else:
            observation = observe()
            reason = evaluate(
                approval, observation, latched, now=time.time(), boot=boot_id()
            )
        if time.monotonic() - started > 10:
            reason = "OBSERVATION_TOO_SLOW"
        allowed = reason == "EGRESS_READY"
        set_lease(allowed)
        if not allowed:
            atomic_root_json(
                LATCH, {"approval_id": approval.get("approval_id"), "reason": reason}
            )
        tick = time.monotonic()
        data = {
            **observation,
            "schema_version": 1,
            "checked_at": time.time(),
            "checked_monotonic": tick,
            "valid_until_monotonic": tick + STATUS_SECONDS,
            "boot_id": boot_id(),
            "reviewed_public_ip": approval.get("reviewed_public_ip"),
            "review_required": not allowed,
            "enforcement_verified": allowed,
            "reason": reason,
        }
        atomic_root_json(STATE, data)
    except Exception:
        # Do not log raw command output/environment. A failed write must also revoke
        # the kernel lease; if revocation fails, its 30-second timeout still bounds it.
        try:
            set_lease(False)
        except Exception:
            pass
        try:
            atomic_root_json(
                LATCH,
                {"approval_id": approval.get("approval_id"), "reason": "UPDATE_FAILED"},
            )
        except Exception:
            pass
        try:
            atomic_root_json(
                STATE,
                {
                    "review_required": True,
                    "enforcement_verified": False,
                    "reason": "UPDATE_FAILED",
                },
            )
        except Exception:
            pass
        return False
    return allowed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["watch", "digest"])
    args = parser.parse_args()
    if args.action == "digest":
        guard = json.loads(run("nft", "-j", "list", "table", "inet", "xianyu_guard"))
        nat = json.loads(run("nft", "-j", "list", "table", "ip", "xianyu_nat"))
        scope = json.loads(run("nft", "-j", "list", "table", "ip", "xianyu_forward_scope"))
        print(rule_digest({"nftables": guard["nftables"] + nat["nftables"] + scope["nftables"]}))
        return
    if os.geteuid() != 0:
        raise SystemExit("ROOT_PRODUCER_REQUIRED")
    trusted_path(Path(__file__))

    def stop(_signal, _frame):
        try:
            set_lease(False)
        finally:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    announced = False
    while True:
        if not update_once():
            print("EGRESS_BLOCKED; MANUAL_REVIEW_REQUIRED", flush=True)
            raise SystemExit(1)
        if not announced:
            print("EGRESS_READY", flush=True)
            announced = True
        time.sleep(5)


if __name__ == "__main__":
    main()
