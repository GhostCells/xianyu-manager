"""Bounded local UDS readiness check; no business calls or application import."""
import http.client
import json
import socket
import time


def main():
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
            assert body['mode'] == 'prepare'
            assert body['runtime_account_id'] == 2
            assert body['business_forbidden'] is True
            assert body['automation_allowed'] is False
            return
        except (OSError, ValueError, AssertionError, http.client.HTTPException):
            time.sleep(1)
        finally:
            connection.close()
    raise SystemExit('PREPARATION_API_NOT_READY')


if __name__ == '__main__':
    main()
