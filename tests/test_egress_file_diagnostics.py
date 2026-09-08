"""No network or privileged IO; diagnostics never expose arbitrary paths."""
import errno
import pytest
from xianyu_manager import egress_control as control


@pytest.mark.parametrize('path,category', [
    ('/proc/123/ns/net', 'runtime_process'),
    ('/proc/123/net/dev', 'runtime_process'),
    ('/proc/123', 'runtime_process'),
    ('/run/netns/' + control.NAMESPACE, 'business_namespace'),
    ('/proc/sys/net/ipv4/ip_forward', 'ipv4_forward_parameter'),
    ('/sys/fs/cgroup/system.slice/example/cgroup.procs', 'cgroup_membership'),
    ('/private/secret-cookie-file', 'unclassified'),
])
def test_file_category_is_redacted_and_stays_hard(path, category):
    result = control.failure_diagnostic(FileNotFoundError(errno.ENOENT, 'secret', path), 'observe')
    assert result['file_category'] == category
    assert result['errno'] == errno.ENOENT
    assert result['transient'] is False
    assert path not in str(result)
    assert 'secret' not in str(result)


def test_permission_error_remains_hard():
    result = control.failure_diagnostic(PermissionError(errno.EACCES, 'secret', '/proc/123/ns/net'), 'observe')
    assert not result['transient']
    assert result['exception_type'] == 'PermissionError'
