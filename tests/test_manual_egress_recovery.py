import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

spec = importlib.util.spec_from_file_location('manual_recovery', Path(__file__).parents[1] / 'scripts/recover_egress.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'RECORD', tmp_path/'record')
    approval = {'approval_id':'a','account_id':2,'lifecycle':'resident_mvp'}
    state = {'reason':'UPDATE_FAILED','transient':True,'stage':'observe','command':'curl','exit_code':28}
    g = SimpleNamespace(APPROVAL=tmp_path/'approval',STATE=tmp_path/'state',LATCH=tmp_path/'latch',
        approval_valid=Mock(return_value=True),boot_id=lambda:'boot',clock_valid=lambda s:s.get('fresh',False),
        observe=Mock(return_value={}),evaluate=Mock(return_value='EGRESS_READY'),atomic_root_json=Mock())
    documents={g.APPROVAL:approval,g.STATE:state,g.LATCH:{'approval_id':'a'},m.RECORD:{'boot_id':'boot','at':0}}
    g.read_root_json=lambda p:documents[p]
    elapsed=[100.0]
    def control(*args):
        if args[0]=='start':
            documents[g.STATE]={'fresh':True,'reason':'EGRESS_READY','approval_id':'a','review_required':False,'enforcement_verified':True,'checked_at':1}
        return 'failed' if args[0]=='show' else ''
    ctl=Mock(side_effect=control)
    def sleep(n):
        elapsed[0]+=n
        documents[g.STATE]['checked_at']=elapsed[0]
    return g,documents,ctl,lambda:elapsed[0],sleep


def run(case):
    g,_,ctl,now,sleep=case
    return m.recover(g,ctl,monotonic=now,sleep=sleep)


def test_transient_recovery_checks_and_waits(case):
    g,docs,ctl,now,_=case
    original=dict(docs[g.APPROVAL])
    assert run(case)['code']=='EGRESS_READY'
    assert now()>=110
    assert docs[g.APPROVAL]==original
    assert [c.args[0] for c in ctl.call_args_list]==['show','show','reset-failed','start']
    assert g.atomic_root_json.call_args.args[0]==m.RECORD


@pytest.mark.parametrize('change',[{'transient':False},{'command':'nft'},{'stage':'write_state'},{'exit_code':22},{'reason':'WRONG_EXIT_NODE'}])
def test_hard_failure_never_starts(case,change):
    g,docs,ctl,_,_=case;docs[g.STATE].update(change)
    assert run(case)['code']=='MANUAL_REVIEW_REQUIRED'
    ctl.assert_not_called();g.observe.assert_not_called()


def test_current_latch_never_cleared(case):
    g,_,ctl,_,_=case;g.LATCH.touch()
    assert run(case)['code']=='EXPLICIT_REVIEW_REQUIRED'
    ctl.assert_not_called();assert g.LATCH.exists()


def test_old_latch_preserved(case):
    g,docs,_,_,_=case;g.LATCH.touch();docs[g.LATCH]={'approval_id':'old'}
    assert run(case)['code']=='EGRESS_READY'
    assert g.LATCH.exists()


def test_expired_approval_never_starts(case):
    g,_,ctl,_,_=case;g.approval_valid.return_value=False
    assert run(case)['code']=='APPROVAL_REQUIRED';ctl.assert_not_called()


@pytest.mark.parametrize('reason',['PUBLIC_ADDRESS_REVIEW_REQUIRED','RULESET_CHANGED','RUNTIME_VERIFIED_FAILED'])
def test_fresh_observation_refusal(case,reason):
    g,_,ctl,_,_=case;g.evaluate.return_value=reason
    assert run(case)['code']==reason
    assert all(c.args[0]=='show' for c in ctl.call_args_list)


def test_cooldown(case):
    g,docs,ctl,_,_=case;m.RECORD.touch();docs[m.RECORD]['at']=90
    assert run(case)['code']=='COOLDOWN';g.observe.assert_not_called()


def test_active_updater_untouched(case):
    g,_,ctl,_,_=case;ctl.side_effect=None;ctl.return_value='active'
    assert run(case)['code']=='UPDATER_BUSY';g.observe.assert_not_called()


def test_approval_changed_during_probe(case):
    g,docs,ctl,_,_=case
    def probe(): docs[g.APPROVAL]={**docs[g.APPROVAL],'approval_id':'new'};return {}
    g.observe.side_effect=probe
    assert run(case)['code']=='APPROVAL_CHANGED'
    assert all(c.args[0]=='show' for c in ctl.call_args_list)


def test_new_hard_latch_during_probe(case):
    g,_,ctl,_,_=case
    def probe():g.LATCH.touch();return {}
    g.observe.side_effect=probe
    assert run(case)['code']=='EXPLICIT_REVIEW_REQUIRED'
    assert all(c.args[0]=='show' for c in ctl.call_args_list)


def test_stale_producer_output_not_success(case):
    g,_,_,_,_=case;g.clock_valid=lambda s:False
    assert run(case)['code']=='RECOVERY_NOT_READY'


def test_single_observation_not_success(case):
    g,docs,ctl,now,_=case
    elapsed=[100.0]
    def sleep(n):elapsed[0]+=n
    assert m.recover(g,ctl,monotonic=lambda:elapsed[0],sleep=sleep)['code']=='RECOVERY_NOT_READY'
