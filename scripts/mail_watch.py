"""Read-only production monitor. No platform calls, recovery API, or service restart."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import smtplib
import ssl
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.request import ProxyHandler, build_opener

STATE_DIR = Path('/var/lib/xianyu-mail-watch')
CONFIG = Path('/etc/xianyu-notifications/smtp.json')
REASONS = {
    'CHROME_DOWN': '独立Chrome服务异常，需要检查。',
    'MANAGER_DOWN': '业务主服务未运行，需要检查。',
    'EGRESS_BLOCKED': '可信出口未就绪；保护保持生效，需审核时不会自动放行。',
    'AUTH_REQUIRED': '消息认证需要人工验证；不会自动扫码。',
    'ACCOUNT_MISMATCH': '运行账号绑定异常，需要检查。',
    'REPLY_DISABLED': '自动回复配置已关闭，需要检查。',
    'DISCONNECTED': '消息连接未就绪；现有自动重连机制继续按原策略运行。',
    'PROBE_FAILED': '本地状态检查失败，无法确认业务是否正常。',
    'HEALTHY': '账号2消息监听和自动回复配置已恢复正常。',
    'TEST': '这是一封配置测试邮件，不代表已制造或恢复任何业务故障。',
}


def classify(health, delivery):
    if health.get('runtime_account_id') != 2:
        return 'ACCOUNT_MISMATCH'
    if health.get('egress', {}).get('ready') is not True:
        return 'EGRESS_BLOCKED'
    if delivery.get('status') == 'verification_required':
        return 'AUTH_REQUIRED'
    if delivery.get('account_id') != 2:
        return 'ACCOUNT_MISMATCH'
    if delivery.get('auto_reply_enabled') is not True:
        return 'REPLY_DISABLED'
    if delivery.get('status') != 'listening' or delivery.get('connected') is not True:
        return 'DISCONNECTED'
    return 'HEALTHY'


def probe():
    for unit, reason in [('xianyu-chrome-account2.service', 'CHROME_DOWN'),
                         ('xianyu-isolated-prepare.service', 'MANAGER_DOWN')]:
        result = subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                                timeout=5, capture_output=True)
        if result.returncode:
            return reason
    # Never use environment proxies for loopback; never log full API responses.
    opener = build_opener(ProxyHandler({}))
    def get(route):
        with opener.open('http://127.0.0.1:8765/api/' + route, timeout=5) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('oversized local response')
        return json.loads(data)
    health = get('health')
    if health.get('egress', {}).get('ready') is not True:
        return 'EGRESS_BLOCKED'
    return classify(health, get('delivery'))


def advance(state, reason, now):
    """Persist incident deduplication; SMTP at most 3 attempts per notification."""
    if reason not in REASONS or reason == 'TEST':
        reason = 'PROBE_FAILED'
    if reason == 'HEALTHY':
        if 'since' not in state:
            return None
        state['healthy_samples'] = state.get('healthy_samples', 0) + 1
        if state['healthy_samples'] < 2:
            return None
        if not state.get('alert_attempted'):
            state.clear()
            return None
        kind = 'recovery'
    else:
        state['healthy_samples'] = 0
        state.setdefault('since', now)
        state['reason'] = reason
        if now < state['since']:
            state['since'] = now
        if now - state['since'] < 60 or state.get('alert_sent'):
            return None
        kind = 'alert'
    attempts = state.get(kind + '_attempts', 0)
    if attempts >= 3:
        if kind == 'recovery':
            state.clear()  # A later, new incident must still be able to alert.
        return None
    if now < state.get(kind + '_next', 0):
        return None
    state[kind + '_attempts'] = attempts + 1
    state[kind + '_next'] = now + (60 if attempts == 0 else 300)
    if kind == 'alert':
        state['alert_attempted'] = True
    return kind


def load_private(path):
    if path.is_symlink():
        raise ValueError('unsafe configuration')
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('unsafe configuration permissions')
    return json.loads(path.read_text())


def send(kind, reason, now):
    config = load_private(CONFIG)
    if (config.get('host') != 'smtp.163.com' or config.get('port') != 465
            or config.get('security') != 'ssl'):
        raise ValueError('unexpected SMTP configuration')
    message = EmailMessage()
    label = {'alert': '运行异常', 'recovery': '运行恢复', 'test': '邮件测试'}[kind]
    message['Subject'] = '[闲鱼账号2] ' + label
    message['From'] = config['sender']
    message['To'] = config['recipient']
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
    message.set_content(f'账号：2\n通知：{label}\n时间UTC：{stamp}\n'
                        f'状态：{reason}\n{REASONS[reason]}\n\n'
                        '本监控只读取状态，不重启Chrome，不修改出口授权，'
                        '不扫描或补发订单，不修改cutoff。'
                        '恢复通知表示监听已恢复，不代表补处理了断线期间的订单。')
    with smtplib.SMTP_SSL(config['host'], config['port'], timeout=10,
                          context=ssl.create_default_context()) as client:
        client.login(config['username'], config['password'])
        refused = client.send_message(message)
        if refused:
            raise RuntimeError('recipient refused')


def save(state):
    fd, temporary = tempfile.mkstemp(dir=STATE_DIR, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATE_DIR / 'state.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test-email', action='store_true')
    args = parser.parse_args()
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    with (STATE_DIR / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = time.time()
        if args.test_email:
            send('test', 'TEST', now)
            print('TEST_SMTP_ACCEPTED')
            return
        path = STATE_DIR / 'state.json'
        state = load_private(path) if path.exists() else {}
        try:
            reason = probe()
        except Exception:
            reason = 'PROBE_FAILED'
        kind = advance(state, reason, now)
        save(state)  # Record attempt before network call to bound uncertain retries.
        if kind:
            try:
                send(kind, 'HEALTHY' if kind == 'recovery' else state['reason'], now)
            except Exception:
                print('SMTP_FAILED_RETRY_BOUNDED')  # Never output SMTP exception details.
            else:
                if kind == 'recovery':
                    state.clear()
                else:
                    state['alert_sent'] = True
                save(state)
                print('SMTP_ACCEPTED_' + kind.upper())
        print('STATE_' + reason)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('MONITOR_FAILED_CHECK_SERVICE')
        raise SystemExit(1)
