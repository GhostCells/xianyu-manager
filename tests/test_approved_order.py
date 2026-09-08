import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from test_fulfillment_a0 import db, setup_order
from xianyu_manager.delivery import DeliveryService
from xianyu_manager.fulfillment_rules import fulfillment_fingerprint
from xianyu_manager.runtime_policy import RuntimePolicy
from xianyu_manager.security import SecretStore

ORDER = '223344556677889900'
ITEM = '1074757605491'
BUYER = '123456789'


@pytest.fixture
def ready(db, tmp_path, monkeypatch):
    account = setup_order(db)
    library = tmp_path / 'library'
    directory = library / '01-test'
    directory.mkdir(parents=True)
    content = b'synthetic delivery bytes'
    (directory / 'test.zip').write_bytes(content)
    db.update_product('01-test', {'listing_url': f'https://www.goofish.com/item?id={ITEM}',
                                'zip_hash': hashlib.sha256(content).hexdigest(), 'zip_size': len(content)}, account)
    with db.connect() as c:
        c.execute('UPDATE accounts SET auto_confirm_delivery=0 WHERE id=?', (account,))
        c.execute('UPDATE products SET zip_hash=?,zip_size=? WHERE dir_name=?',
                  (hashlib.sha256(content).hexdigest(), len(content), '01-test'))
        c.execute('INSERT INTO chat_sessions(account_id,chat_id,buyer_id,listing_item_id) VALUES (?,?,?,?)',
                  (account, '667788', BUYER, ITEM))
    db.confirm_product_share('01-test', fulfillment_fingerprint(db.get_product('01-test')))
    policy = RuntimePolicy(account_id=account, mvp_fulfillment=True, fulfillment_items=(ITEM,),
                           order_cutoff_at='2026-09-08T00:51:38Z')
    monkeypatch.setattr(RuntimePolicy, 'require_egress', lambda self: None)
    service = DeliveryService(tmp_path/'profiles', None, db, SecretStore(tmp_path/'secret'), runtime_policy=policy)
    service._status = 'listening'
    service._account_id = account
    service._runtime_websocket = object()
    service._runtime_cookie_map = {}
    service._runtime_seller_id = '987654321'
    row = dict(order_id=ORDER, item_id=ITEM, buyer_id=BUYER, order_status='待发货', paid_time='2026-09-08T06:46:06Z')
    fetch = AsyncMock(return_value={'orders': [row]})
    monkeypatch.setattr(service, '_fetch_recent_sold_orders', fetch)
    monkeypatch.setattr(service, '_send_text', AsyncMock())
    monkeypatch.setattr(service, '_notify', lambda *a: None)
    return service, account, library, row, fetch


def test_preview_is_read_only_and_targeted(ready, db):
    s, a, lib, row, fetch = ready
    with db.connect() as c:
        before = '\n'.join(c.iterdump())
    result = asyncio.run(s.approved_order(a, ORDER, library=lib))
    assert result['precheck_passed'] and not result['executed']
    fetch.assert_awaited_once_with({}, exact_order_id=ORDER)
    s._send_text.assert_not_awaited()
    with db.connect() as c:
        assert '\n'.join(c.iterdump()) == before
    assert not s._event_tasks


def test_execute_reuses_real_claim_and_send_once(ready, db):
    s, a, lib, row, fetch = ready
    async def run():
        result = await s.approved_order(a, ORDER, library=lib, execute=True)
        assert result['delivery_status'] == 'delivered'
        with pytest.raises(ValueError, match='ALREADY_RECORDED'):
            await s.approved_order(a, ORDER, library=lib, execute=True)
    asyncio.run(run())
    s._send_text.assert_awaited_once()
    order = db.get_order(ORDER)
    assert order['delivery_attempts'] == 1 and order['message_sent_at'] and order['delivery_message_hash']
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM automation_outbound_events WHERE kind='delivery' AND reference=? AND status='sent'", ('order:'+ORDER,)).fetchone()[0] == 1
    assert all(call.kwargs == {'exact_order_id': ORDER} for call in fetch.await_args_list)
    assert not s._event_tasks


@pytest.mark.parametrize('case', ['before','equal','other_account','other_item','delivered','claim','outbound',
                                 'unverified','not_paid','disabled','manual','disk','multi_zip','missing_time','nonunique'])
def test_rejections_never_send(ready, db, case):
    s,a,lib,row,fetch=ready
    if case=='before':row['paid_time']='2026-09-08T00:51:37Z'
    if case=='equal':row['paid_time']='2026-09-08T00:51:38Z'
    if case=='other_account':a+=999
    if case=='other_item':row['item_id']='9999999999999'
    if case in ('delivered','claim'):
        db.upsert_paid_order(order_id=ORDER,account_id=a,product_dir_name='01-test',listing_item_id=ITEM,buyer_id=BUYER,chat_id='chat',event_fingerprint='synthetic')
        with db.connect() as c:c.execute('UPDATE orders SET delivery_status=? WHERE xianyu_order_id=?', ('delivered' if case=='delivered' else 'sending',ORDER))
    if case=='outbound':
        with db.connect() as c:c.execute("INSERT INTO automation_outbound_events(account_id,kind,reference,status) VALUES (?,'delivery',?,'sent')",(a,'order:'+ORDER))
    if case=='unverified':db.update_product('01-test',{'share_code':'changed'},a)
    if case=='not_paid':row['order_status']='待付款'
    if case=='disabled':
        with db.connect() as c:c.execute('UPDATE accounts SET delivery_enabled=0 WHERE id=?',(a,))
    if case=='manual':
        with db.connect() as c:c.execute("UPDATE chat_sessions SET manual_takeover_until='2999-01-01' WHERE account_id=?",(a,))
    if case=='disk':(lib/'01-test'/'test.zip').write_bytes(b'changed')
    if case=='multi_zip':(lib/'01-test'/'extra.zip').write_bytes(b'other')
    if case=='missing_time':row['paid_time']=None
    if case=='nonunique':fetch.return_value={'orders':[row,row]}
    expected = {'before':'ORDER_AT_OR_BEFORE_CUTOFF','equal':'ORDER_AT_OR_BEFORE_CUTOFF',
                'other_account':'MISMATCH','other_item':'FULFILLMENT_ITEM_NOT_ALLOWED',
                'delivered':'ALREADY_RECORDED','claim':'ALREADY_RECORDED','outbound':'OUTBOUND_EXISTS',
                'unverified':'PRODUCT_NOT_QUALIFIED','not_paid':'NOT_PENDING_SHIP',
                'disabled':'DELIVERY_DISABLED','manual':'ACCOUNT_OR_MANUAL_BLOCK',
                'disk':'PACKAGE_CHANGED','multi_zip':'PACKAGE_AMBIGUOUS',
                'missing_time':'PAYMENT_TIME_MISSING','nonunique':'NOT_UNIQUE'}
    with pytest.raises((ValueError,RuntimeError), match=expected[case]):
        asyncio.run(s.approved_order(a,ORDER,library=lib,execute=True))
    s._send_text.assert_not_awaited()
    assert not s._event_tasks


def test_history_reconcile_stays_forbidden(ready):
    s,a,lib,_,_=ready
    async def run():
        with pytest.raises(ValueError,match='MVP_HISTORICAL_RECONCILIATION_FORBIDDEN'):
            await s.reconcile_order(ORDER)
        s._schedule_order_recovery(object(),a,'987654321',{})
        assert not s._event_tasks
    asyncio.run(run())


def test_concurrent_approved_calls_only_send_once(ready):
    s,a,lib,_,_=ready
    async def run():
        results = await asyncio.gather(
            s.approved_order(a,ORDER,library=lib,execute=True),
            s.approved_order(a,ORDER,library=lib,execute=True), return_exceptions=True)
        assert sum(isinstance(r, ValueError) for r in results) == 1
    asyncio.run(run())
    s._send_text.assert_awaited_once()


@pytest.mark.parametrize('unexpected', [False,True])
def test_target_request_never_falls_back_to_list(ready,monkeypatch,unexpected):
    s,a,lib,row,_=ready
    monkeypatch.delattr(s,'_fetch_recent_sold_orders')
    raw={'commonData':{'orderId':ORDER,'paySuccessTime':'2026-09-08 14:46:06'},'buyerInfoVO':{}}
    mock=AsyncMock(return_value={'ret':['SUCCESS'],'data':{'module':{'items':[raw,raw] if unexpected else [raw]}}})
    monkeypatch.setattr(s,'_post_mtop',mock)
    async def run():
        if unexpected:
            with pytest.raises(ValueError,match='TARGET_QUERY_NOT_UNIQUE'):
                await s._fetch_recent_sold_orders({'_m_h5_tk':'synthetic'},exact_order_id=ORDER)
        else:await s._fetch_recent_sold_orders({'_m_h5_tk':'synthetic'},exact_order_id=ORDER)
    asyncio.run(run())
    assert json.loads(mock.await_args.kwargs['data_json'])['orderIds']==ORDER
    assert mock.await_count==1


def test_api_requires_explicit_header_and_single_id(ready,monkeypatch):
    from fastapi.testclient import TestClient
    from xianyu_manager import app as api
    s,a,lib,_,_=ready
    monkeypatch.setattr(api,'runtime_policy',s.runtime_policy)
    mock=AsyncMock(return_value={'precheck_passed':True})
    monkeypatch.setattr(api.delivery_service,'approved_order',mock)
    client=TestClient(api.app,base_url='http://127.0.0.1')
    body={'account_id':a,'order_id':ORDER}
    assert client.post('/api/delivery/approved-order/execute',json=body).status_code==403
    headers={'X-Order-Action':'approved-single-order'}
    assert client.post('/api/delivery/approved-order/preview',json={**body,'order_ids':[ORDER]},headers=headers).status_code==422
    assert client.post('/api/delivery/approved-order/preview',json=body,headers=headers).status_code==200
    assert mock.await_args.kwargs['execute'] is False
