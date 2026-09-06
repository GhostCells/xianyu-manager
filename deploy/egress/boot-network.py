"""Root-owned cold-boot reconstruction. Never grants or renews an egress lease."""
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path('/etc/xianyu-egress')


def start(run, trusted):
    # Refuse to adopt partially configured networks or silently widen forwarding.
    for name in ('host-forward.nft', 'guard.nft', 'apply-network.sh', 'window-id'):
        trusted(ROOT / name)
    assert run('sysctl', '-n', 'net.ipv4.ip_forward') == '0', 'FORWARDING_BASELINE_CHANGED'
    links = json.loads(run('ip', '-j', 'link', 'show'))
    assert not any(x['ifname'] in ('xmg-host', 'xmg-net') for x in links)
    assert not json.loads(run('ip', '-j', 'netns', 'list')), 'UNREVIEWED_NAMESPACE'
    assert not any(x.get('priority') == 1000 for x in json.loads(run('ip', '-j', '-4', 'rule', 'show')))
    tables = json.loads(run('nft', '-j', 'list', 'tables'))['nftables']
    assert not any(x.get('table', {}).get('name') in
                   ('xianyu_guard', 'xianyu_nat', 'xianyu_forward_scope') for x in tables)
    run('ip', 'link', 'show', 'tailscale0')
    run('nft', '--check', '--file', str(ROOT / 'host-forward.nft'))
    run('nft', '--check', '--file', str(ROOT / 'guard.nft'))
    # Failure at any later step retains scope protection. No automatic cleanup.
    run('nft', '--file', str(ROOT / 'host-forward.nft'))
    run('sysctl', '-q', '-w', 'net.ipv4.ip_forward=1')
    run(str(ROOT / 'apply-network.sh'), '--approved-window', (ROOT / 'window-id').read_text().strip())


if __name__ == '__main__':
    assert os.getuid() == 0
    spec = importlib.util.spec_from_file_location('rollback', ROOT / 'rollback-window.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.trusted(ROOT / 'rollback-window.py')
    start(module.run, module.trusted)
