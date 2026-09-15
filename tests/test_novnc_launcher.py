import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('viewer', Path(__file__).parents[1] / 'scripts/novnc_launcher.py')
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


@pytest.fixture
def web(monkeypatch):
    tunnel = viewer.Tunnel(demo=True)
    cls = viewer.handler(tunnel)
    instance = cls.__new__(cls)
    instance.headers = {'Host': '127.0.0.1:18768'}
    replies = []
    instance.reply = lambda *args: replies.append(args)
    return instance, tunnel, replies


def test_page_and_status(web):
    instance, _, replies = web
    instance.path = '/'
    instance.do_GET()
    assert '连接闲鱼云端画面' in replies[-1][1].decode()
    instance.path = '/status'
    instance.do_GET()
    assert replies[-1][1]['identity'] == viewer.IDENTITY


@pytest.mark.parametrize('headers', [{}, {'Origin': 'https://evil.invalid'},
    {'Origin': 'http://127.0.0.1:18768', 'X-Viewer-Token': 'bad'},
    {'Host': 'evil.invalid', 'X-Viewer-Token': viewer.TOKEN}])
def test_reject_unauthorized(web, headers):
    instance, _, replies = web
    instance.headers.update(headers)
    instance.path = '/connect'
    instance.do_POST()
    assert replies[-1][0] == 403


def test_connect_demo(web, monkeypatch):
    instance, tunnel, replies = web
    monkeypatch.setattr(viewer.subprocess, 'Popen', lambda *a, **k: pytest.fail('no SSH in demo'))
    monkeypatch.setattr(tunnel, 'connect', tunnel._connect)
    instance.headers.update({'Origin': viewer.URL, 'X-Viewer-Token': viewer.TOKEN})
    instance.path = '/connect'
    instance.do_POST()
    assert replies[-1][0] == 202
    assert tunnel.demo_ready


def test_existing_connection_reused(monkeypatch):
    tunnel = viewer.Tunnel()
    monkeypatch.setattr(viewer, 'page_ready', lambda: True)
    monkeypatch.setattr(viewer.subprocess, 'Popen', lambda *a, **k: pytest.fail('must reuse'))
    tunnel._connect()
    assert '复用' in tunnel.message


def test_busy_port_not_killed(monkeypatch):
    tunnel = viewer.Tunnel()
    monkeypatch.setattr(viewer, 'page_ready', lambda: False)
    monkeypatch.setattr(viewer, 'port_busy', lambda: True)
    monkeypatch.setattr(viewer.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not spawn'))
    tunnel._connect()
    assert '占用' in tunnel.message


def test_ssh_failure_not_stuck(monkeypatch):
    class Failed:
        def poll(self): return 255
    monkeypatch.setattr(viewer, 'page_ready', lambda: False)
    monkeypatch.setattr(viewer, 'port_busy', lambda: False)
    monkeypatch.setattr(viewer.subprocess, 'Popen', lambda *a, **k: Failed())
    tunnel = viewer.Tunnel()
    tunnel._connect()
    assert not tunnel.connecting
    assert 'SSH连接失败' in tunnel.message


def test_connect_dedup(monkeypatch):
    tunnel = viewer.Tunnel()
    tunnel.connecting = True
    monkeypatch.setattr(viewer.threading, 'Thread', lambda *a, **k: pytest.fail('duplicate'))
    tunnel.connect()


def test_fixed_safe_ssh():
    assert viewer.SSH[-1] == 'xianyu-cloud'
    assert '-N' in viewer.SSH
    assert 'StrictHostKeyChecking=yes' in viewer.SSH
    assert '127.0.0.1:18767:127.0.0.1:6080' in viewer.SSH
    assert not any('9222' in a or 'restart' in a or 'systemctl' in a for a in viewer.SSH)
