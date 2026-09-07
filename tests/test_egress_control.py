"""Synthetic producer/consumer tests: no privileged commands, network or assets."""

import importlib.util
import json
from pathlib import Path
import time
import subprocess

import pytest
from xianyu_manager import egress_control as control
from xianyu_manager import runtime_policy as policy_module


def approval():
    return {
        "approved": True,
        "approval_id": "synthetic-review-1",
        "exit_node_ip": control.EXIT_NODE,
        "reviewed_public_ip": "8.8.8.8",
        "boot_id": "test-boot",
        "expires_at": 1000,
        "rules_sha256": "a" * 64,
        "fault_report_sha256": "b" * 64,
    }


def observation():
    return {
        "exit_node_ip": control.EXIT_NODE,
        "observed_public_ip": "8.8.8.8",
        "client_running": True,
        "routes_ready": True,
        "namespace_verified": True,
        "runtime_verified": True,
        "no_flow_offload": True,
        "rules_sha256": "a" * 64,
        "path_kind": "relay",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("exit_node_ip", "100.1.1.1"),
        ("client_running", False),
        ("routes_ready", False),
        ("namespace_verified", False),
        ("runtime_verified", False),
        ("no_flow_offload", False),
        ("rules_sha256", "c" * 64),
        ("observed_public_ip", "8.8.4.4"),
        ("observed_public_ip", None),
    ],
)
def test_observation_failure_cannot_grant(field, value):
    sample = {**observation(), field: value}
    assert (
        control.evaluate(approval(), sample, None, now=100, boot="test-boot")
        != "EGRESS_READY"
    )


def test_relay_accepted_but_same_approval_cannot_rearm():
    assert (
        control.evaluate(approval(), observation(), None, now=100, boot="test-boot")
        == "EGRESS_READY"
    )
    assert (
        control.evaluate(
            approval(), observation(), "synthetic-review-1", now=100, boot="test-boot"
        )
        == "EXPLICIT_REVIEW_REQUIRED"
    )
    assert (
        control.evaluate(approval(), observation(), None, now=100, boot="next-boot")
        == "APPROVAL_REQUIRED"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved", False),
        ("expires_at", 50),
        ("expires_at", float("nan")),
        ("reviewed_public_ip", None),
        ("reviewed_public_ip", "127.0.0.1"),
        ("fault_report_sha256", ""),
        ("rules_sha256", ""),
        ("approval_id", ""),
    ],
)
def test_approval_not_self_issued(field, value):
    assert not control.approval_valid(
        {**approval(), field: value}, now=100, boot="test-boot"
    )


@pytest.mark.parametrize(
    "wall,mono,boot,expected",
    [
        (101, 11, "test", True),
        (125, 35, "test", False),
        (99, 11, "test", False),
        (101, 9, "test", False),
        (101, 11, "other", False),
        (101, 30, "test", False),
    ],
)
def test_clock_expiry_and_reboot(wall, mono, boot, expected):
    sample = {
        "schema_version": 1,
        "checked_at": 100,
        "checked_monotonic": 10,
        "valid_until_monotonic": 30,
        "boot_id": "test",
    }
    assert control.clock_valid(sample, wall=wall, mono=mono, boot=boot) == expected


def test_business_owned_forged_file_and_symlinks_rejected(tmp_path):
    file = tmp_path / "status.json"
    file.write_text(json.dumps(observation()))
    file.chmod(0o644)
    # Even tests running as root cannot trust a path beneath world-writable /tmp.
    with pytest.raises(ValueError):
        control.read_root_json(file)
    link = tmp_path / "linked.json"
    link.symlink_to(file)
    with pytest.raises(ValueError):
        control.read_root_json(link)
    p = policy_module.RuntimePolicy(
        prepare_mode=True, account_id=2, login_authorized=True, egress_status_path=file
    )
    assert not p.egress_status()["ready"]
    with pytest.raises(policy_module.RuntimeOperationBlocked):
        p.require_login(2)


def test_rules_hash_ignores_only_dynamic_lease_and_counters():
    a = {
        "nftables": [
            {
                "set": {
                    "name": "lease",
                    "table": "xianyu_guard",
                    "handle": 1,
                    "elem": ["10.203.0.2"],
                }
            }
        ]
    }
    b = {
        "nftables": [
            {"set": {"name": "lease", "table": "xianyu_guard", "handle": 9, "elem": []}}
        ]
    }
    assert control.rule_digest(a) == control.rule_digest(b)
    b["nftables"].append({"rule": {"expr": [{"accept": None}]}})
    assert control.rule_digest(a) != control.rule_digest(b)


@pytest.fixture
def producer(monkeypatch, tmp_path):
    writes = {}
    leases = []
    auth = {**approval(), "expires_at": time.time() + 600}
    monkeypatch.setattr(control, "APPROVAL", tmp_path / "approval")
    monkeypatch.setattr(control, "LATCH", tmp_path / "latch")
    monkeypatch.setattr(control, "STATE", tmp_path / "status")
    monkeypatch.setattr(control, "read_root_json", lambda p: auth)
    monkeypatch.setattr(control, "boot_id", lambda: "test-boot")
    monkeypatch.setattr(control, "observe", observation)
    monkeypatch.setattr(control, "set_lease", leases.append)
    monkeypatch.setattr(
        control, "atomic_root_json", lambda p, d: writes.update({p: dict(d)})
    )
    return writes, leases


def test_updater_success_links_short_lifetime(producer):
    writes, leases = producer
    assert control.update_once()
    assert leases == [True]
    status = writes[control.STATE]
    assert status["enforcement_verified"] and not status["review_required"]
    assert control.clock_valid(status, boot="test-boot")


def test_update_failure_revokes_and_marks_blocked(producer, monkeypatch):
    writes, leases = producer
    monkeypatch.setattr(
        control, "observe", lambda: (_ for _ in ()).throw(TimeoutError())
    )
    assert not control.update_once()
    assert leases == [False]
    assert writes[control.LATCH]["reason"] == "UPDATE_FAILED"
    assert not writes[control.STATE]["enforcement_verified"]


def test_transient_curl_failure_revokes_then_can_reobserve(producer, monkeypatch):
    writes, leases = producer
    original = control.observe
    def fail():
        raise subprocess.CalledProcessError(28, ['curl','secret-url'], stderr='SECRET')
    monkeypatch.setattr(control,'observe',fail)
    assert not control.update_once()
    assert leases == [False]
    assert writes[control.STATE]['transient'] is True
    assert control.LATCH not in writes
    assert 'SECRET' not in json.dumps(writes[control.STATE])
    monkeypatch.setattr(control,'observe',original)
    assert control.update_once()
    assert leases == [False, True]


@pytest.mark.parametrize('command,code,stage,transient', [
    ('curl',6,'observe',True),('curl',28,'observe',True),
    ('curl',22,'observe',False),('nft',1,'write_lease',False),
    ('tailscale',1,'observe',False),('curl',28,'write_state',False)])
def test_only_known_transport_errors_retry(command,code,stage,transient):
    d=control.failure_diagnostic(subprocess.CalledProcessError(code,[command,'SECRET'],stderr='SECRET'),stage)
    assert d['transient'] is transient
    assert 'SECRET' not in json.dumps(d)


def test_status_write_failure_revokes_just_renewed_lease(producer, monkeypatch):
    _, leases = producer
    monkeypatch.setattr(
        control,
        "atomic_root_json",
        lambda *a: (_ for _ in ()).throw(OSError("synthetic disk full")),
    )
    assert not control.update_once()
    assert leases == [True, False]


def test_existing_application_gate_accepts_only_trusted_current_contract(
    producer, monkeypatch, tmp_path
):
    writes, _ = producer
    assert control.update_once()
    file = tmp_path / "trusted-fixture"
    file.touch()
    monkeypatch.setattr(
        policy_module, "read_root_json", lambda _: writes[control.STATE]
    )
    p = policy_module.RuntimePolicy(
        prepare_mode=True, account_id=2, login_authorized=True, egress_status_path=file
    )
    with pytest.raises(policy_module.RuntimeOperationBlocked, match='PREPARATION_PERMISSION_REQUIRED'):
        p.require_login(2)
    writes[control.STATE].update(purpose='manual_login_inventory', account_id=2,
                                 operations=['login', 'inventory_once'])
    p.require_login(2)
    with pytest.raises(policy_module.RuntimeOperationBlocked, match="PREPARE_BUSINESS"):
        p.require_business()
    writes[control.STATE]["checked_at"] = 0
    with pytest.raises(policy_module.RuntimeOperationBlocked):
        p.require_login(2)
    writes[control.STATE]["checked_at"] = time.time()
    with pytest.raises(policy_module.RuntimeOperationBlocked):
        p.require_login(2)


def test_scoped_deployment_contract():
    root = Path(__file__).resolve().parents[1] / "deploy/egress"
    nft = (root / "guard.nft").read_text()
    assert "flush ruleset" not in nft and "policy drop" not in nft
    assert "meta skuid" not in nft  # never block all ubuntu management processes
    assert nft.count("meta nfproto ipv6 drop") == 2
    for line in nft.splitlines():
        if " accept" in line and "policy accept" not in line:
            assert "@lease" in line
    assert "ct state { established, related } accept" in nft
    assert 'iifname "xmg-host" drop' in nft and 'oifname "xmg-host" drop' in nft
    assert "flags timeout; timeout 30s" in nft
    assert "39.191.10.92" not in nft
    service = (root / "xianyu-isolated-prepare.service").read_text()
    assert "User=xianyu-runtime" in service and "NetworkNamespacePath=" in service
    assert (
        "RestrictNamespaces=user pid net" in service and "CapabilityBoundingSet=\n" in service
    )
    assert "TemporaryFileSystem=/tmp:mode=1777 /run" in service
    updater = (root / "xianyu-egress.service").read_text()
    assert "Restart=on-failure" in updater and "RestartPreventExitStatus=1" in updater
    assert "StartLimitBurst=3" in updater


def test_uds_launch_is_opt_in_and_does_not_remove_login_lock(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/run_preparation_service.py"
    spec = importlib.util.spec_from_file_location("uds_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("XIANYU_MANAGER_API_UDS", raising=False)
    assert module.listen_arguments() == ["--host", "127.0.0.1", "--port", "8765"]
    monkeypatch.setenv("XIANYU_MANAGER_API_UDS", "/run/xianyu-runtime/api.sock")
    assert module.listen_arguments() == ["--uds", "/run/xianyu-runtime/api.sock"]
    monkeypatch.setenv("XIANYU_MANAGER_API_UDS", "/tmp/untrusted.sock")
    with pytest.raises(ValueError):
        module.listen_arguments()
    for policy in [policy_module.RuntimePolicy(),
                   policy_module.RuntimePolicy(safe_mode=True, prepare_mode=True),
                   policy_module.RuntimePolicy(prepare_mode=True, login_authorized=True)]:
        with pytest.raises(SystemExit):
            module.validate_policy(policy)
    module.validate_policy(policy_module.RuntimePolicy(prepare_mode=True, account_id=2, login_authorized=True))
    with pytest.raises(policy_module.RuntimeOperationBlocked):
        policy_module.RuntimePolicy(prepare_mode=True, account_id=2, login_authorized=True).require_login(2)


def test_import_does_not_require_unix_pwd_on_windows(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pwd", None)
    spec = importlib.util.spec_from_file_location(
        "portable_egress_import", control.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.STATUS_SECONDS == 20


def test_nested_namespace_must_have_only_loopback(tmp_path):
    net = tmp_path / 'net'
    net.mkdir()
    dev = net / 'dev'
    dev.write_text('header\nheader\n lo: 0 0 0\n')
    assert control.offline_sandbox_namespace(tmp_path)
    dev.write_text('header\nheader\n lo: 0 0 0\n eth0: 0 0 0\n')
    assert not control.offline_sandbox_namespace(tmp_path)
    dev.write_text('header\nheader\n')
    assert not control.offline_sandbox_namespace(tmp_path)


@pytest.mark.parametrize("script", ["apply-network.sh", "rollback-network.sh"])
def test_network_scripts_refuse_before_commands_without_window(script, tmp_path):
    import subprocess

    path = Path(__file__).resolve().parents[1] / "deploy/egress" / script
    for args in ([], ["--approved-window"]):
        result = subprocess.run(
            ["/bin/sh", str(path), *args],
            env={"PATH": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1 and not result.stdout and not result.stderr
