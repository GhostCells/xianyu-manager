"""Pure fake-command rollback checks: no host IO, network or business imports."""
import importlib.util
from pathlib import Path
import json
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rollback', ROOT / 'deploy/egress/rollback-window.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.mark.parametrize('populated', [False, True, 'partial'])
@pytest.mark.parametrize('failure', [None, 'sysctl', 'pids'])
@pytest.mark.parametrize('persistent', [False, True])
def test_restore_before_delete_and_repeat(monkeypatch, tmp_path, populated, failure, persistent):
    env = tmp_path / 'env'
    env.write_text('XIANYU_MANAGER_ACCOUNT_ID=2\nXIANYU_MANAGER_PREPARE_MODE=true\nXIANYU_MANAGER_LOGIN_AUTHORIZED=false\n')
    import hashlib
    manifest = dict(exclusive_objects_prechecked_absent=True, original_ip_forward='0',
                    tables=[list(x) for x in mod.TABLES], units=mod.UNITS,
                    namespace='xianyu-business', host_interface='xmg-host', restore_sysctls={},
                    original_environment=str(env), live_environment=str(env),
                    environment_sha256=hashlib.sha256(env.read_bytes()).hexdigest())
    manifest['persistent_preparation'] = persistent
    calls = []
    def fake(*args):
        calls.append(args)
        if failure == 'sysctl' and args[:2] == ('sysctl', '-q'):
            raise RuntimeError('synthetic restore failure')
        if failure == 'pids' and args[:3] == ('ip', 'netns', 'pids'):
            return '123'
        if args[:4] == ('systemctl', 'show', args[2], '-p'):
            return 'not-found' if args[4] == 'LoadState' else '0'
        if args == ('nft', '-j', 'list', 'tables'):
            tables = mod.TABLES[:1] if populated == 'partial' else mod.TABLES
            return json.dumps({'nftables': [{'table':dict(family=f,name=t)} for f,t in tables] if populated else []})
        if args == ('ip', '-j', 'netns', 'list'):
            return '[{"name":"xianyu-business"}]' if populated else '[]'
        if args in [('ip','-j','link','show'), ('ip','-j','-4','rule','show')]: return '[]'
        if args == ('sysctl', '-n', 'net.ipv4.ip_forward'): return '0'
        return ''
    monkeypatch.setattr(mod, 'run', fake)
    monkeypatch.setattr(mod, 'trusted', Path)
    if failure == 'sysctl' or (failure == 'pids' and populated):
        with pytest.raises((RuntimeError, AssertionError)):
            mod.rollback(manifest)
        assert not any(x[:3] == ('nft', 'delete', 'table') for x in calls)
        return
    mod.rollback(manifest)
    mod.rollback(manifest)
    for i,args in enumerate(calls):
        if args[:3] == ('nft','delete','table'):
            assert ('sysctl','-n','net.ipv4.ip_forward') in calls[:i]
    assert not any('flush ruleset' in ' '.join(x) for x in calls)
    if persistent:
        assert ('systemctl', 'enable', 'xianyu-preparation.service') in calls


def test_scope_is_ipv4_and_priorities_are_distinct():
    text=(ROOT/'deploy/egress/host-forward.nft').read_text()
    assert 'table ip xianyu_forward_scope' in text
    assert 'hook forward priority -175' in text
    assert 'hook input' not in text and 'hook output' not in text
    rules=[x.strip() for x in text.splitlines() if 'counter accept' in x]
    assert len(rules)==2
    assert all('iifname' in x and 'oifname' in x and '10.203.0.2' in x for x in rules)


@pytest.mark.parametrize('rule,expected', [
    ({'dst':'10.203.0.0','dstlen':30,'table':'main'}, True),
    ({'dst':'10.203.0.0/30','table':'main'}, True),
    ({'dst':'10.203.0.0','dstlen':24,'table':'main'}, False),
    ({'dst':'10.203.0.0/30','table':52}, False),
])
def test_actual_iproute_json_preserves_exact_ownership(rule, expected):
    assert mod.is_owned_return_rule(rule) == expected
