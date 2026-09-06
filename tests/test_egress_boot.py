"""Fake commands only: boot cannot turn forwarding on before scope protection."""
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('boot', Path(__file__).resolve().parents[1] / 'deploy/egress/boot-network.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.mark.parametrize('fail', [None, 'scope', 'apply'])
@pytest.mark.parametrize('namespace_output', ['', '[]'])
def test_protection_precedes_forwarding_no_lease(monkeypatch, tmp_path, fail, namespace_output):
    monkeypatch.setattr(mod, 'ROOT', tmp_path)
    (tmp_path / 'window-id').write_text('synthetic')
    calls = []
    def run(*args):
        calls.append(args)
        if args == ('sysctl', '-n', 'net.ipv4.ip_forward'): return '0'
        if args == ('ip', '-j', 'netns', 'list'): return namespace_output
        if args[:2] == ('ip', '-j'): return '[]'
        if args[:2] == ('nft', '-j'): return '{"nftables": []}'
        if fail == 'scope' and args[:2] == ('nft', '--file'): raise RuntimeError()
        if fail == 'apply' and args[0].endswith('apply-network.sh'): raise RuntimeError()
        return ''
    if fail:
        with pytest.raises(RuntimeError): mod.start(run, lambda p: p)
    else:
        mod.start(run, lambda p: p)
    enable = ('sysctl', '-q', '-w', 'net.ipv4.ip_forward=1')
    scope = ('nft', '--file', str(tmp_path / 'host-forward.nft'))
    if enable in calls: assert calls.index(scope) < calls.index(enable)
    if fail == 'scope': assert enable not in calls
    assert not any('delete' in x or 'flush' in x or 'element' in x for x in calls)


def test_refuses_existing_forwarding():
    calls = []
    def run(*args):
        calls.append(args)
        return '1'
    with pytest.raises(AssertionError, match='FORWARDING_BASELINE_CHANGED'):
        mod.start(run, lambda p: p)
    assert len(calls) == 1
