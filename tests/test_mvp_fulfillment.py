import asyncio
from unittest.mock import AsyncMock
import pytest
from xianyu_manager.runtime_policy import RuntimePolicy, RuntimeOperationBlocked
from xianyu_manager.delivery import DeliveryService
from xianyu_manager.security import SecretStore
from test_fulfillment_a0 import db, setup_order


def policy():
    return RuntimePolicy(mvp_fulfillment=True, fulfillment_items=('123456789012',),
                         order_cutoff_at='2026-09-07T00:00:00Z')


def test_mvp_recovery_and_selection_remain_disabled():
    p=policy()
    assert p.fulfillment_enabled and not p.order_recovery_enabled
    with pytest.raises(RuntimeOperationBlocked): p.require_selection()
    p.require_delivery_item('123456789012')
    for item in ['', None, '123456789013']:
        with pytest.raises(RuntimeOperationBlocked): p.require_delivery_item(item)


def test_mvp_no_historical_or_group_paths(db):
    account=setup_order(db)
    s=DeliveryService(db.path.parent/'profiles',None,db,SecretStore(db.path.parent/'secret'),runtime_policy=policy())
    s._account_id=account
    s._recover_recent_paid_orders=AsyncMock(side_effect=AssertionError('recovery'))
    s._schedule_order_recovery(None,account,'seller',{})
    s._recover_recent_paid_orders.assert_not_called()
    with pytest.raises(ValueError,match='MVP_HISTORICAL'):
        asyncio.run(s.reconcile_order('123456789012'))
    with pytest.raises(ValueError,match='MVP_HISTORICAL'):
        asyncio.run(s.retry_platform_confirmation('123456789012',account,{}))
    asyncio.run(s._process_group_waiting_event(account,'seller',{},{}))


def test_mvp_config_requires_cutoff_and_allowlist(monkeypatch):
    from xianyu_manager.config import read_runtime_options
    for k,v in {'XIANYU_MANAGER_MVP_FULFILLMENT':'true','XIANYU_MANAGER_RESIDENT_REPLY':'true',
        'XIANYU_MANAGER_REPLY_ONLY':'false','XIANYU_MANAGER_PREPARE_MODE':'false',
        'XIANYU_MANAGER_ACCOUNT_ID':'2','XIANYU_MANAGER_LOGIN_AUTHORIZED':'true',
        'XIANYU_MANAGER_FULFILLMENT_ITEMS':'123456789012','XIANYU_MANAGER_ORDER_CUTOFF_AT':''}.items():monkeypatch.setenv(k,v)
    with pytest.raises(ValueError):read_runtime_options()
    monkeypatch.setenv('XIANYU_MANAGER_ORDER_CUTOFF_AT','2026-09-07T00:00:00Z')
    assert read_runtime_options()['fulfillment_items']==('123456789012',)
    monkeypatch.setenv('XIANYU_MANAGER_FULFILLMENT_ITEMS','')
    with pytest.raises(ValueError):read_runtime_options()


def test_final_delivery_blocks_non_allowlisted_item(db):
    account=setup_order(db)
    s=DeliveryService(db.path.parent/'profiles',None,db,SecretStore(db.path.parent/'secret'),runtime_policy=policy())
    s._account_id=account
    s._send_text=AsyncMock(side_effect=AssertionError('must not send'))
    with pytest.raises(RuntimeOperationBlocked,match='ITEM_NOT_ALLOWED'):
        asyncio.run(s._guarded_send_text(None,account_id=account,kind='delivery',reference='order:missing',chat_id='chat',buyer_id='buyer',seller_id='seller',text='synthetic',payment_time='2026-09-08T00:00:00Z'))
    s._send_text.assert_not_called()
