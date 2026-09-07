"""Bounded local UDS readiness check; no business calls or application import."""
import http.client
import json
import socket
import time
import sys


def main(*, reply_only=False, mvp=False):
    for _ in range(45):
        connection = http.client.HTTPConnection('localhost', timeout=2)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect('/run/xianyu-runtime/api.sock')
            connection.sock = sock
            connection.request('GET', '/api/health')
            response = connection.getresponse()
            body = json.loads(response.read())
            assert response.status == 200
            assert body['mode'] == ('normal' if reply_only or mvp else 'prepare')
            assert body['runtime_account_id'] == 2
            if mvp:
                assert body['mvp_fulfillment'] is True
                assert body['fulfillment_enabled'] is True
                assert body['order_recovery_enabled'] is False
                assert body['order_cutoff_configured'] is True
                assert body['fulfillment_items']
            elif reply_only:
                assert body['reply_only'] is True
                assert body['fulfillment_enabled'] is False
                assert body['order_recovery_enabled'] is False
            else:
                assert body['business_forbidden'] is True
                assert body['automation_allowed'] is False
            return
        except (OSError, ValueError, AssertionError, http.client.HTTPException):
            time.sleep(1)
        finally:
            connection.close()
    raise SystemExit('PREPARATION_API_NOT_READY')


if __name__ == '__main__':
    if sys.argv[1:] not in ([], ['--reply-only'], ['--mvp']):
        raise SystemExit('UNSUPPORTED_READINESS_MODE')
    main(reply_only=sys.argv[1:] == ['--reply-only'], mvp=sys.argv[1:] == ['--mvp'])
