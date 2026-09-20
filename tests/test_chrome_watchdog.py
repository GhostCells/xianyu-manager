import threading
import unittest
from unittest.mock import Mock, patch
from xianyu_manager.chrome_watchdog import watch_browser, probe_browser

class WatchdogTests(unittest.TestCase):
    def run_sequence(self, values):
        stop = Mock()
        stop.wait.side_effect = [False] * len(values) + [True]
        terminate = Mock()
        watch_browser(threading.Event(), probe=Mock(side_effect=values),
                      terminate=terminate, shutdown=stop)
        return terminate

    def test_three_failures_exit(self):
        self.run_sequence([False, False, False]).assert_called_once_with(1)

    def test_success_resets_failure_count(self):
        self.run_sequence([False, False, True, False, False]).assert_not_called()

    def test_crash_does_not_wait_for_cdp_failure(self):
        crashed = threading.Event()
        crashed.set()
        stop = Mock()
        stop.wait.return_value = False
        terminate = Mock()
        watch_browser(crashed, probe=Mock(side_effect=AssertionError),
                      terminate=terminate, shutdown=stop)
        terminate.assert_called_once_with(1)

    def test_connection_error_closes_socket(self):
        with patch('xianyu_manager.chrome_watchdog.http.client.HTTPConnection') as factory:
            factory.return_value.request.side_effect = OSError()
            self.assertFalse(probe_browser())
            factory.return_value.close.assert_called_once()

    def test_missing_page_is_unhealthy(self):
        with patch('xianyu_manager.chrome_watchdog.http.client.HTTPConnection') as factory:
            response = Mock(status=200)
            response.read.side_effect = [b'{"webSocketDebuggerUrl":"ws://localhost/test"}', b'[]']
            factory.return_value.getresponse.return_value = response
            self.assertFalse(probe_browser())
