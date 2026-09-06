"""Fail-closed, manifest-scoped rollback. Install root-owned; never run Git as root."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def trusted(path):
    path = Path(path)
    for part in (path, *path.parents):
        info = part.lstat()
        if part.is_symlink() or info.st_uid or info.st_mode & 0o022:
            raise ValueError('UNTRUSTED_ROLLBACK_INPUT')
    return path


TABLES = [('inet', 'xianyu_guard'), ('ip', 'xianyu_nat'), ('ip', 'xianyu_forward_scope')]
UNITS = ['xianyu-egress.service', 'xianyu-isolated-prepare.service',
         'xianyu-api.socket', 'xianyu-api.service', 'xianyu-network-test.service']


def rollback(manifest):
    # Manifest was recorded only after proving all declared objects absent.
    assert manifest['exclusive_objects_prechecked_absent'] is True
    assert manifest['original_ip_forward'] == '0'
    assert manifest['tables'] == [list(x) for x in TABLES]
    assert manifest['units'] == UNITS
    assert manifest['namespace'] == 'xianyu-business'
    assert manifest['host_interface'] == 'xmg-host'
    for unit in UNITS:
        state = run('systemctl', 'show', unit, '-p', 'LoadState', '--value')
        if state != 'not-found':
            run('systemctl', 'stop', unit)
            assert run('systemctl', 'show', unit, '-p', 'MainPID', '--value') in ('0', '')
    tables = json.loads(run('nft', '-j', 'list', 'tables'))['nftables']
    present = {(x['table']['family'], x['table']['name']) for x in tables if 'table' in x}
    if ('inet', 'xianyu_guard') in present:
        run('nft', 'flush', 'set', 'inet', 'xianyu_guard', 'lease')
    namespaces = {x['name'] for x in json.loads(run('ip', '-j', 'netns', 'list'))}
    if 'xianyu-business' in namespaces:
        assert not run('ip', 'netns', 'pids', 'xianyu-business'), 'BUSINESS_STILL_RUNNING'
    # No protection is removed until forwarding and affected values are restored.
    run('sysctl', '-q', '-w', 'net.ipv4.ip_forward=0')
    for key, value in manifest['restore_sysctls'].items():
        assert re.fullmatch(r'net\.ipv4\.conf\.(all|default|lo|eth0|tailscale0)\.[a-z_]+', key)
        assert re.fullmatch(r'-?\d+', value)
        run('sysctl', '-q', '-w', key + '=' + value)
    assert run('sysctl', '-n', 'net.ipv4.ip_forward') == '0'
    for key, value in manifest['restore_sysctls'].items():
        assert run('sysctl', '-n', key) == value, 'SYSCTL_RESTORE_FAILED'
    links = json.loads(run('ip', '-j', 'link', 'show'))
    if any(x['ifname'] == 'xmg-host' for x in links):
        run('ip', 'link', 'del', 'xmg-host')
    # A crash may leave the peer in the host namespace before it is moved.
    if any(x['ifname'] == 'xmg-net' for x in json.loads(run('ip', '-j', 'link', 'show'))):
        run('ip', 'link', 'del', 'xmg-net')
    rules = json.loads(run('ip', '-j', '-4', 'rule', 'show'))
    for rule in rules:
        if rule.get('priority') == 1000:
            assert rule.get('dst') == '10.203.0.0/30' and rule.get('table') in ('main', 254)
            run('ip', '-4', 'rule', 'del', 'priority', '1000', 'to', '10.203.0.0/30', 'lookup', 'main')
    if 'xianyu-business' in namespaces:
        run('ip', 'netns', 'del', 'xianyu-business')
    for family, table in TABLES:
        if (family, table) in present:
            run('nft', 'delete', 'table', family, table)
    env = trusted(manifest['original_environment'])
    values = dict(line.split('=', 1) for line in env.read_text().splitlines()
                  if '=' in line and not line.lstrip().startswith('#'))
    assert values['XIANYU_MANAGER_ACCOUNT_ID'] == '2'
    assert values['XIANYU_MANAGER_PREPARE_MODE'] == 'true'
    assert values['XIANYU_MANAGER_LOGIN_AUTHORIZED'] == 'false'
    # Original config is never rewritten by installation. It remains ubuntu-owned
    # in existing deployments; caller must instead provide a root-owned validated
    # copy and verify its hash against the live EnvironmentFile before restoration.
    import hashlib
    live = Path(manifest['live_environment'])
    assert hashlib.sha256(live.read_bytes()).hexdigest() == manifest['environment_sha256']
    run('systemctl', 'start', 'xianyu-preparation-display.service', 'xianyu-preparation.service')


if __name__ == '__main__':
    assert os.getuid() == 0
    assert len(sys.argv) == 3 and sys.argv[1] == '--manifest'
    manifest = trusted(sys.argv[2])
    rollback(json.loads(manifest.read_text()))
