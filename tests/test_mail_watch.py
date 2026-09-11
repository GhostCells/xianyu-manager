import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('mail_watch', Path(__file__).parents[1] / 'scripts/mail_watch.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


@pytest.mark.parametrize('key,value,expected', [
    ('status', 'verification_required', 'AUTH_REQUIRED'),
    ('status', 'reconnecting', 'DISCONNECTED'),
    ('connected', False, 'DISCONNECTED'),
    ('account_id', 1, 'ACCOUNT_MISMATCH'),
    ('auto_reply_enabled', False, 'REPLY_DISABLED'),
])
def test_classification(key, value, expected):
    health = {'runtime_account_id': 2, 'egress': {'ready': True}}
    delivery = dict(account_id=2, status='listening', connected=True, auto_reply_enabled=True)
    assert watch.classify(health, delivery) == 'HEALTHY'
    delivery[key] = value
    assert watch.classify(health, delivery) == expected


def test_egress_before_auth():
    assert watch.classify({'runtime_account_id': 2, 'egress': {'ready': False}}, {}) == 'EGRESS_BLOCKED'


def test_short_failure_silent():
    state = {}
    assert watch.advance(state, 'DISCONNECTED', 100) is None
    assert watch.advance(state, 'HEALTHY', 130) is None
    assert watch.advance(state, 'HEALTHY', 160) is None
    assert state == {}


def test_persistent_failure_dedup_recovery():
    state = {}
    watch.advance(state, 'DISCONNECTED', 100)
    assert watch.advance(state, 'DISCONNECTED', 159) is None
    assert watch.advance(state, 'DISCONNECTED', 160) == 'alert'
    state['alert_sent'] = True
    assert watch.advance(state, 'AUTH_REQUIRED', 300) is None
    assert watch.advance(state, 'HEALTHY', 330) is None
    assert watch.advance(state, 'HEALTHY', 360) == 'recovery'


def test_bounded_smtp_failures():
    state = {}
    watch.advance(state, 'DISCONNECTED', 0)
    assert watch.advance(state, 'DISCONNECTED', 60) == 'alert'
    assert watch.advance(state, 'DISCONNECTED', 90) is None
    assert watch.advance(state, 'DISCONNECTED', 120) == 'alert'
    assert watch.advance(state, 'DISCONNECTED', 419) is None
    assert watch.advance(state, 'DISCONNECTED', 420) == 'alert'
    assert watch.advance(state, 'DISCONNECTED', 1000) is None


def test_recovery_failure_does_not_suppress_future_incident():
    state = dict(since=0, alert_attempted=True, alert_sent=True, recovery_attempts=3)
    watch.advance(state, 'HEALTHY', 100)
    watch.advance(state, 'HEALTHY', 130)
    assert state == {}
    watch.advance(state, 'DISCONNECTED', 160)
    assert watch.advance(state, 'DISCONNECTED', 220) == 'alert'


def test_clock_backwards():
    state = dict(since=500)
    assert watch.advance(state, 'DISCONNECTED', 100) is None
    assert state['since'] == 100


def test_no_business_mutations():
    text = Path(watch.__file__).read_text()
    for forbidden in ('/execute', '/start', '/confirm', 'POST', 'sqlite3', 'systemctl restart'):
        assert forbidden not in text


def test_private_config(tmp_path, monkeypatch):
    path = tmp_path / 'smtp.json'
    path.write_text('{}')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        watch.load_private(path)


def test_smtp_payload_excludes_runtime_data(monkeypatch):
    captured = []
    config = dict(host='smtp.163.com', port=465, security='ssl', sender='sender@example.com',
                  recipient='recipient@example.com', username='sender@example.com', password='test-secret')
    monkeypatch.setattr(watch, 'load_private', lambda p: config)
    class SMTP:
        def __init__(self, *a, **kw):
            assert kw['timeout'] == 10
            assert kw['context'].check_hostname
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, user, secret): assert secret == 'test-secret'
        def send_message(self, message):
            captured.append(str(message))
            return {}
    monkeypatch.setattr(watch.smtplib, 'SMTP_SSL', SMTP)
    watch.send('alert', 'AUTH_REQUIRED', 0)
    assert len(captured) == 1
    assert 'test-secret' not in captured[0]


def test_failed_service_does_not_activate_api(monkeypatch):
    class Result: returncode = 3
    monkeypatch.setattr(watch.subprocess, 'run', lambda *a, **kw: Result())
    monkeypatch.setattr(watch, 'build_opener', lambda *a: pytest.fail('must not call API'))
    assert watch.probe() == 'CHROME_DOWN'
