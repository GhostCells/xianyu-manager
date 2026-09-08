import subprocess
from dataclasses import replace
import pytest
from test_egress_control import producer, observation
from xianyu_manager import egress_control as c
from xianyu_manager.egress_recovery import RecoveryGate, recovery_allowed
from xianyu_manager.runtime_policy import RuntimePolicy


def setup(producer,monkeypatch):
    writes,leases=producer
    auth=c.read_root_json(c.APPROVAL)
    monkeypatch.setattr(c,'read_root_json',lambda p: auth if p==c.APPROVAL else writes[p])
    assert c.update_once()
    first=dict(writes[c.STATE])
    def failed(*,probe_public=True):
        if not probe_public:return observation()
        raise subprocess.CalledProcessError(28,['curl'])
    monkeypatch.setattr(c,'observe',failed)
    return writes,leases,first


def test_two_failures_do_not_renew_or_revoke_then_recover(producer,monkeypatch):
    writes,leases,first=setup(producer,monkeypatch)
    for count in (1,2):
        assert c.update_once()
        assert writes[c.STATE]['reason']=='EGRESS_DEGRADED'
        assert writes[c.STATE]['consecutive_transient_failures']==count
        assert writes[c.STATE]['checked_at']==first['checked_at']
        assert writes[c.STATE]['valid_until_monotonic']==first['valid_until_monotonic']
        assert leases==[True]
    monkeypatch.setattr(c,'observe',observation)
    assert c.update_once() and leases==[True,True]


def test_third_failure_revokes(producer,monkeypatch):
    writes,leases,_=setup(producer,monkeypatch)
    assert c.update_once() and c.update_once()
    assert not c.update_once()
    assert leases==[True,False]
    assert not writes[c.STATE]['enforcement_verified']


def test_expired_state_gets_no_grace(producer,monkeypatch):
    writes,leases,_=setup(producer,monkeypatch)
    writes[c.STATE]['valid_until_monotonic']=0
    assert not c.update_once() and leases==[True,False]


@pytest.mark.parametrize('field,value',[('observed_public_ip','8.8.4.4'),('rules_sha256','c'*64)])
def test_hard_failure_immediate(producer,monkeypatch,field,value):
    writes,leases,_=setup(producer,monkeypatch)
    monkeypatch.setattr(c,'observe',lambda:{**observation(),field:value})
    assert not c.update_once() and leases==[True,False]
    assert c.LATCH in writes


def test_recovery_once_and_cooldown():
    g=RecoveryGate();g.block()
    assert not g.ready(checked_at=1,now=0)
    assert not g.ready(checked_at=1,now=11)
    assert g.ready(checked_at=2,now=12)
    assert not g.ready(checked_at=3,now=20)
    g.block()
    assert not g.ready(checked_at=4,now=21)
    assert not g.ready(checked_at=5,now=40)
    assert g.ready(checked_at=6,now=73)


def test_recovery_never_enables_fulfillment():
    p=RuntimePolicy(account_id=2,reply_only=True,resident_reply=True)
    assert recovery_allowed(p)
    for q in [replace(p,reply_only=False),replace(p,account_id=1),replace(p,prepare_mode=True)]:
        assert not recovery_allowed(q)
    assert recovery_allowed(replace(p,order_cutoff_at='2026-01-01T00:00:00Z'))
    candidate=replace(p,reply_only=False,mvp_fulfillment=True,order_cutoff_at='2026-01-01T00:00:00Z')
    assert recovery_allowed(candidate,delivery_enabled=False)
    assert not recovery_allowed(candidate,delivery_enabled=True)
    assert not p.fulfillment_enabled and not p.order_recovery_enabled
