"""Bounded runtime-loopback readiness only; never create a browser or session."""
import socket
import time


def wait_ready(connect=socket.create_connection, sleep=time.sleep):
    for _ in range(30):
        try:
            with connect(('127.0.0.1', 9222), timeout=1):
                return
        except OSError:
            sleep(1)
    raise SystemExit('INDEPENDENT_CHROME_NOT_READY')


if __name__ == '__main__':
    wait_ready()
