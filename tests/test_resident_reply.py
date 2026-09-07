from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from xianyu_manager.resident_reply import restore_reply_owner
from xianyu_manager import egress_control as control


@pytest.mark.parametrize('detected,enabled,expected', [
    (False, True, 'manual_verification_required'),
    (True, False, 'owner_ready_reply_disabled'),
    (True, True, 'reply_start_requested'),
])
def test_resident_owner_only(detected, enabled, expected):
    policy = SimpleNamespace(resident_reply=True, mode='normal', reply_only=True,
        fulfillment_enabled=False, order_recovery_enabled=False, account_id=2,
        egress_status=lambda: {'ready': True})
    session = SimpleNamespace(start_login=AsyncMock(return_value={'login_detected': detected}), confirm_login=AsyncMock())
    delivery = SimpleNamespace(start_auto_reply=AsyncMock())
    db = SimpleNamespace(get_auto_reply_settings=Mock(return_value={'enabled': enabled}))
    assert asyncio.run(restore_reply_owner(policy, session, delivery, db)) == expected
    assert session.confirm_login.await_count == int(detected)
    assert delivery.start_auto_reply.await_count == int(detected and enabled)


def test_no_browser_before_egress():
    p=SimpleNamespace(resident_reply=True, mode='normal', reply_only=True,
        fulfillment_enabled=False, order_recovery_enabled=False, account_id=2,
        egress_status=lambda: {'ready': False})
    session=SimpleNamespace(start_login=AsyncMock())
    assert asyncio.run(restore_reply_owner(p, session, None, None, wait=AsyncMock())) == 'egress_not_ready'
    session.start_login.assert_not_called()


@pytest.mark.parametrize('change,allowed', [({}, True), ({'approved': False}, False),
    ({'account_id':1},False), ({'operations':['login','reply','delivery']},False),
    ({'boot_id':'other'},False), ({'expires_at':999},False)])
def test_resident_authorization_still_scoped(change, allowed):
    a=dict(approved=True, approval_id='resident', exit_node_ip=control.EXIT_NODE,
        reviewed_public_ip='8.8.8.8', boot_id='boot', expires_at=None,
        rules_sha256='a'*64, fault_report_sha256='b'*64,
        lifecycle='resident_reply', account_id=2, operations=['login','reply'])
    assert control.approval_valid({**a,**change},now=100000,boot='boot') == allowed
