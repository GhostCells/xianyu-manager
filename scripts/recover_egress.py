"""Fixed manual recovery for transient updater failures; install root-owned.

Never changes approval, latch, leases, business settings or browser processes.
The existing producer alone may grant fresh leases after its own checks.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

UNIT = 'xianyu-egress.service'
RECORD = Path('/var/lib/xianyu-egress/manual-recovery.json')
LOCK = Path('/var/lib/xianyu-egress/manual-recovery.lock')


def eligible(g, approval, state):
    if not g.approval_valid(approval, now=time.time(), boot=g.boot_id()):
        return 'APPROVAL_REQUIRED'
    if approval.get('account_id') != 2 or approval.get('lifecycle') not in {'resident_reply', 'resident_mvp'}:
        return 'APPROVAL_REQUIRED'
    if g.LATCH.exists():
        latch = g.read_root_json(g.LATCH)
        if latch.get('approval_id') == approval['approval_id']:
            return 'EXPLICIT_REVIEW_REQUIRED'
    if not (state.get('reason') == 'UPDATE_FAILED' and state.get('transient') is True
            and state.get('stage') == 'observe' and state.get('command') == 'curl'
            and (state.get('exit_code') in {5, 6, 7, 28, 35, 52, 55, 56}
                 or state.get('exception_type') == 'TimeoutExpired')):
        return 'MANUAL_REVIEW_REQUIRED'
    return None


def recover(g, control, *, sleep=time.sleep, monotonic=time.monotonic):
    approval = g.read_root_json(g.APPROVAL)
    state = g.read_root_json(g.STATE)
    if (g.clock_valid(state) and state.get('reason') == 'EGRESS_READY'
            and state.get('review_required') is False and state.get('enforcement_verified') is True):
        return {'code': 'ALREADY_READY'}
    reason = eligible(g, approval, state)
    if reason:
        return {'code': reason}
    active = control('show', UNIT, '-p', 'ActiveState', '--value')
    if active not in {'failed', 'inactive'}:
        return {'code': 'UPDATER_BUSY'}
    if RECORD.exists():
        old = g.read_root_json(RECORD)
        if old.get('boot_id') == g.boot_id() and monotonic() - old.get('at', 0) < 60:
            return {'code': 'COOLDOWN'}
    g.atomic_root_json(RECORD, {'boot_id': g.boot_id(), 'at': monotonic()})
    # Full fresh observation includes pinned exit/public IP, routes, rules and UID ownership.
    observation = g.observe()
    reason = g.evaluate(approval, observation, None, now=time.time(), boot=g.boot_id())
    if reason != 'EGRESS_READY':
        return {'code': reason}
    # Reject concurrent approval, hard-latch or state changes before touching the unit.
    if g.read_root_json(g.APPROVAL) != approval:
        return {'code': 'APPROVAL_CHANGED'}
    reason = eligible(g, approval, g.read_root_json(g.STATE))
    if reason:
        return {'code': reason}
    if control('show', UNIT, '-p', 'ActiveState', '--value') not in {'failed', 'inactive'}:
        return {'code': 'UPDATER_BUSY'}
    control('reset-failed', UNIT)
    control('start', UNIT)
    # Observe genuine producer output; never synthesize or extend trusted status.
    deadline = monotonic() + 40
    since = None
    samples = set()
    while monotonic() < deadline:
        s = g.read_root_json(g.STATE)
        if (g.clock_valid(s) and s.get('reason') == 'EGRESS_READY'
                and s.get('approval_id') == approval['approval_id']
                and s.get('review_required') is False and s.get('enforcement_verified') is True):
            if since is None:
                since = monotonic()
            samples.add(s.get('checked_at'))
            if len(samples) >= 2 and monotonic() - since >= 10:
                return {'code': 'EGRESS_READY'}
        else:
            since = None
            samples.clear()
        sleep(2)
    return {'code': 'RECOVERY_NOT_READY'}


def main():
    if os.geteuid() != 0:
        raise SystemExit('ROOT_REQUIRED')
    path = Path('/usr/local/lib/xianyu-egress/egress_control.py')
    # Do not import privileged code from a user checkout or inherited PYTHONPATH.
    import stat
    for p in (path, *path.parents, Path(__file__).resolve()):
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise SystemExit('UNTRUSTED_CODE')
    spec = importlib.util.spec_from_file_location('trusted_egress', path)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    g.trusted_path(LOCK.parent, regular=False)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({'code': 'UPDATER_BUSY'}))
        return
    def control(*args):
        return subprocess.run(['/usr/bin/systemctl', *args], check=True, capture_output=True,
            text=True, timeout=15, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C'}).stdout.strip()
    import signal
    def timeout_handler(*_):
        raise TimeoutError()
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(150)
    try:
        result = recover(g, control)
    except Exception:
        result = {'code': 'CHECK_FAILED'}  # Never expose command output or secrets.
    signal.alarm(0)
    print(json.dumps(result))
    os.close(fd)


if __name__ == '__main__':
    main()
