import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from test_approved_order import ready, db, ORDER, ITEM, BUYER
from xianyu_manager.live_group_watch import LiveGroupWatch


@pytest.fixture
def watching(ready):
    s,a,lib,row,fetch=ready
    s._reply_listen_started_ms=int(time.time()*1000)-1000
    watch=LiveGroupWatch(s,lib)
    watch.INTERVAL=0
    watch.MAX_PROBES=3
    return watch,s,a,lib,row,fetch


def test_wait_then_manual_group_completion_uses_normal_chain_once(watching,db):
    w,s,a,lib,row,fetch=watching
    fetch.side_effect=[{'orders':[{**row,'order_status':'待成团'}]},
                       {'orders':[row]}, {'orders':[row]}, {'orders':[row]}]
    async def run():
        w.schedule(a,s._runtime_websocket,ORDER,ITEM,BUYER,int(time.time()*1000))
        task=w.tasks[ORDER]
        w.schedule(a,s._runtime_websocket,ORDER,ITEM,BUYER,int(time.time()*1000))
        assert w.tasks[ORDER] is task
        await task
        w.schedule(a,s._runtime_websocket,ORDER,ITEM,BUYER,int(time.time()*1000))
        assert ORDER not in w.tasks
    asyncio.run(run())
    s._send_text.assert_awaited_once()
    order=db.get_order(ORDER)
    assert order['delivery_status']=='delivered' and order['delivery_attempts']==1
    assert all(c.kwargs=={'exact_order_id':ORDER} for c in fetch.await_args_list)
    assert not s._event_tasks
    assert not s.runtime_policy.order_recovery_enabled


@pytest.mark.parametrize('case',['missing_timestamp','old_event','future_event','wrong_account','disabled','reply_only','invalid_id'])
def test_only_live_approved_account_cards_admitted(watching,db,case):
    w,s,a,_,_,fetch=watching
    timestamp=int(time.time()*1000);order=ORDER
    if case=='missing_timestamp':timestamp=None
    if case=='old_event':timestamp=s._reply_listen_started_ms
    if case=='future_event':timestamp+=60000
    if case=='wrong_account':a=1
    if case=='reply_only':s.runtime_policy=replace(s.runtime_policy,reply_only=True)
    if case=='disabled':
        with db.connect() as c:c.execute('UPDATE accounts SET delivery_enabled=0 WHERE id=?',(a,))
    if case=='invalid_id':order='bad'
    async def run():
        w.schedule(a,s._runtime_websocket,order,ITEM,BUYER,timestamp)
        assert not w.tasks
    asyncio.run(run())
    fetch.assert_not_awaited();s._send_text.assert_not_awaited()


@pytest.mark.parametrize('case',['cutoff_before','cutoff_equal','missing_time','cancelled','identity','product','package','manual','duplicate','ambiguous','timeout'])
def test_real_gates_stop_without_send(watching,db,case):
    w,s,a,lib,row,fetch=watching
    if case=='cutoff_before':row['paid_time']='2026-09-08T00:00:00Z'
    if case=='cutoff_equal':row['paid_time']=s.runtime_policy.order_cutoff_at
    if case=='missing_time':row['paid_time']=None
    if case=='cancelled':row['order_status']='交易关闭'
    if case=='identity':row['buyer_id']='99999999'
    if case=='product':db.update_product('01-test',{'share_code':'changed'},a)
    if case=='package':(lib/'01-test/test.zip').write_bytes(b'changed')
    if case=='manual':
        with db.connect() as c:c.execute("UPDATE chat_sessions SET manual_takeover_until='2999-01-01'")
    if case=='duplicate':
        db.upsert_paid_order(order_id=ORDER,account_id=a,product_dir_name='01-test',listing_item_id=ITEM,buyer_id=BUYER,chat_id='667788',event_fingerprint='other event')
    if case=='ambiguous':fetch.return_value={'orders':[row,row]}
    if case=='timeout':fetch.side_effect=asyncio.TimeoutError()
    asyncio.run(w.run(a,s._runtime_websocket,ORDER,ITEM,BUYER))
    s._send_text.assert_not_awaited()


def test_unknown_state_bounded_and_never_delivered(watching):
    w,s,a,_,row,fetch=watching
    row['order_status']='unknown'
    asyncio.run(w.run(a,s._runtime_websocket,ORDER,ITEM,BUYER))
    assert fetch.await_count==3
    s._send_text.assert_not_awaited()


def test_time_limit_independent_of_probe_limit(watching):
    w,s,a,_,_,fetch=watching
    w.MAX_SECONDS=0
    asyncio.run(w.run(a,s._runtime_websocket,ORDER,ITEM,BUYER))
    fetch.assert_not_awaited()


def test_session_change_cancels_followup(watching):
    w,s,a,_,row,fetch=watching
    async def query(*args,**kwargs):
        s._runtime_websocket=object()
        return {'orders':[row]}
    fetch.side_effect=query
    asyncio.run(w.run(a,s._runtime_websocket,ORDER,ITEM,BUYER))
    s._send_text.assert_not_awaited()


def test_capacity_and_stop_do_not_leave_recovery_tasks(watching):
    w,s,a,_,_,fetch=watching
    w.MAX_ACTIVE=1
    async def run():
        blocker=asyncio.Event()
        async def query(*args,**kwargs):await blocker.wait()
        fetch.side_effect=query
        w.schedule(a,s._runtime_websocket,ORDER,ITEM,BUYER,int(time.time()*1000))
        w.schedule(a,s._runtime_websocket,'999999999999999999',ITEM,BUYER,int(time.time()*1000))
        assert list(w.tasks)==[ORDER]
        await w.stop()
        assert not w.tasks and not w.seen and not s._event_tasks
    asyncio.run(run())
    s._send_text.assert_not_awaited()


def test_missing_local_chat_resolved_only_after_platform_ready(watching,db,monkeypatch):
    w,s,a,_,row,fetch=watching
    with db.connect() as c:c.execute('DELETE FROM chat_sessions')
    create=AsyncMock(return_value='667788');monkeypatch.setattr(s,'_create_chat',create)
    fetch.side_effect=[{'orders':[{**row,'order_status':'待成团'}]}, {'orders':[row]}, {'orders':[row]}, {'orders':[row]}]
    asyncio.run(w.run(a,s._runtime_websocket,ORDER,ITEM,BUYER))
    create.assert_awaited_once();s._send_text.assert_awaited_once()


def test_websocket_waiting_card_routes_to_live_watch(watching,monkeypatch):
    import json
    from unittest.mock import Mock
    from test_delivery import group_card_event
    from xianyu_manager import delivery
    w,s,a,_,_,_=watching
    event=group_card_event('我已小刀，待刀成')
    event['timestamp']=int(time.time()*1000)
    monkeypatch.setattr(delivery,'decode_sync_payload',lambda _:event)
    schedule=Mock();monkeypatch.setattr(s._live_group_watch,'schedule',schedule)
    ws=Mock()
    ws.recv=AsyncMock(side_effect=[json.dumps({'body':{'syncPushPackage':{'data':[{'data':'fixture'}]}}}),asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(s._listen(ws,a,'987654321',{}))
    schedule.assert_called_once()
    assert schedule.call_args.args[0:3]==(a,ws,ORDER)
    assert not s._event_tasks


@pytest.mark.parametrize('changed',[False,True])
def test_cloud_tutorial_uses_registered_version_not_zip_requirement(watching,db,changed):
    from xianyu_manager.delivery_package import tree_hash
    from xianyu_manager.fulfillment_rules import fulfillment_fingerprint
    w,s,a,lib,_,_=watching
    folder=lib/'01-test'
    (folder/'test.zip').unlink()
    (folder/'guide.txt').write_text('synthetic knowledge')
    with db.connect() as c:
        c.execute("UPDATE products SET delivery_kind='cloud',zip_name='',zip_hash='',zip_size=0,delivery_revision=? WHERE dir_name='01-test'",(tree_hash(folder),))
    db.confirm_product_share('01-test',fulfillment_fingerprint(db.get_product('01-test')))
    if changed:(folder/'guide.txt').write_text('changed')
    asyncio.run(w.run(a,s._runtime_websocket,ORDER,ITEM,BUYER))
    if changed:s._send_text.assert_not_awaited()
    else:s._send_text.assert_awaited_once()
