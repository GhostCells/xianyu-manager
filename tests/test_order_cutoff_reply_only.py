import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from test_fulfillment_a0 import db, setup_order
from xianyu_manager.delivery import DeliveryService
from xianyu_manager.order_cutoff import OrderCutoffBlocked, require_after_cutoff
from xianyu_manager.runtime_policy import RuntimePolicy, RuntimeOperationBlocked
from xianyu_manager.security import SecretStore


@pytest.mark.parametrize('value', [None, '', 'bad', '2026-09-07 08:00:00', True, '123'])
def test_missing_or_ambiguous_payment_time(value):
    with pytest.raises(OrderCutoffBlocked):
        require_after_cutoff('2026-09-07T00:00:00Z', value)


@pytest.mark.parametrize('payment,allowed', [
    ('2026-09-06T23:59:59Z', False), ('2026-09-07T00:00:00Z', False),
    ('2026-09-07T08:00:00+08:00', False), ('2026-09-07T08:00:01+08:00', True),
    ('1788739201000', True), ('1788739201', True),
])
def test_strict_boundary_and_utc(payment, allowed):
    if allowed:
        assert require_after_cutoff('2026-09-07T00:00:00Z', payment).tzinfo == timezone.utc
    else:
        with pytest.raises(OrderCutoffBlocked):
            require_after_cutoff('2026-09-07T00:00:00Z', payment)


def make_service(db, policy):
    account = setup_order(db)
    service = DeliveryService(db.path.parent / 'profiles', None, db, SecretStore(db.path.parent / 'secret'), runtime_policy=policy)
    service._account_id = account
    service._runtime_seller_id = '111111'
    service._runtime_cookie_map = {}
    service._runtime_websocket = object()
    service._status = 'listening'
    return service, account


@pytest.mark.parametrize('path', ['live', 'recovery', 'manual'])
@pytest.mark.parametrize('case', ['before', 'equal', 'after', 'missing', 'invalid', 'unverified', 'mapping', 'duplicate', 'existing_old'])
def test_all_delivery_routes(db, monkeypatch, path, case):
    now = int(time.time())
    policy = RuntimePolicy(order_cutoff_at=str(now - 5))
    service, account = make_service(db, policy)
    item, order = '1068148111818', '223344556677889900'
    db.update_product('01-test', {'listing_url': f'https://www.goofish.com/item?id={item}'}, account)
    if case == 'unverified':
        db.update_product('01-test', {'share_code': 'changed'}, account)
    if case == 'mapping':
        item = '1068148111819'
    event = dict(orderId=order, itemId=item, buyerId='998877', sid='667788', timestamp=now * 1000)
    if case == 'existing_old':
        db.upsert_paid_order(order_id=order, account_id=account, product_dir_name='01-test', listing_item_id=item, buyer_id='998877', chat_id='667788', event_fingerprint='synthetic')
    paid = {'before': now - 10, 'existing_old': now - 10, 'equal': now - 5, 'missing': '', 'invalid': '2026-09-07 15:00:00'}.get(case, now)
    fetch = AsyncMock(return_value={'orders': [dict(order_id=order, item_id=item, buyer_id='998877', order_status='待发货', paid_time=paid, create_time=now)]})
    monkeypatch.setattr(service, '_fetch_recent_sold_orders', fetch)
    monkeypatch.setattr(service, '_create_chat', AsyncMock(return_value='667788'))
    send = AsyncMock()
    monkeypatch.setattr(service, '_send_text', send)
    monkeypatch.setattr(service, '_confirm_platform_delivery', AsyncMock())
    monkeypatch.setattr(service, '_outbound_preflight', AsyncMock(return_value={'allowed': True}))
    async def run():
        if path == 'live':
            await service._process_paid_event(object(), account, '111111', {}, event)
        elif path == 'recovery':
            await service._recover_recent_paid_orders(object(), account, '111111', {})
        else:
            try:
                await service.reconcile_order(order)
            except ValueError:
                assert case in {'unverified', 'mapping'}
    asyncio.run(run())
    assert send.await_count == (1 if case in {'after', 'duplicate'} else 0)
    if case == 'duplicate':
        asyncio.run(run())
        assert send.await_count == 1


@pytest.mark.parametrize('cutoff', ['', 'bad', '2026-09-07 08:00:00'])
def test_full_mode_missing_cutoff_fails_before_io(db, cutoff):
    service, account = make_service(db, RuntimePolicy(order_cutoff_at=cutoff))
    with pytest.raises(OrderCutoffBlocked):
        asyncio.run(service._process_paid_event(object(), account, '111111', {}, {}))
    with pytest.raises(OrderCutoffBlocked):
        asyncio.run(service.start(account))


def test_reply_only_blocks_all_order_entry_points_before_io(db, monkeypatch):
    service, account = make_service(db, RuntimePolicy(reply_only=True))
    async def forbidden(*args, **kwargs):
        raise AssertionError('reply-only reached order/network side effect')
    for name in ('_fetch_recent_sold_orders', '_send_text', '_create_chat', '_post_mtop'):
        monkeypatch.setattr(service, name, forbidden)
    async def run():
        service._schedule_order_recovery(object(), account, '111111', {})
        assert not service._event_tasks
        await service._recover_recent_paid_orders(object(), account, '111111', {})
        await service._process_paid_event(object(), account, '111111', {}, {})
        await service._process_group_waiting_event(account, '111111', {}, {})
        for action in (
            service.reconcile_order('223344556677889900'),
            service.retry_platform_confirmation('order', account, {}),
            service._confirm_platform_delivery('order', {}),
            service._free_group_order('order', '12345678', '998877', {}),
            service._guarded_send_text(object(), account_id=account, kind='delivery', reference='order:order', chat_id='chat', buyer_id='998877', seller_id='111111', text='synthetic'),
        ):
            with pytest.raises(RuntimeOperationBlocked, match='REPLY_ONLY'):
                await action
    asyncio.run(run())
    with pytest.raises(RuntimeOperationBlocked, match='SELECTION'):
        service.runtime_policy.require_selection()


def test_restart_reads_same_explicit_policy(monkeypatch):
    from xianyu_manager.config import read_runtime_options
    monkeypatch.setenv('XIANYU_MANAGER_REPLY_ONLY', 'true')
    monkeypatch.setenv('XIANYU_MANAGER_ACCOUNT_ID', '2')
    monkeypatch.delenv('XIANYU_MANAGER_ORDER_CUTOFF_AT', raising=False)
    for _ in range(2):
        policy = RuntimePolicy(**read_runtime_options())
        assert policy.reply_only
        assert not policy.fulfillment_enabled and not policy.order_recovery_enabled
        assert policy.order_cutoff_at == ''
    monkeypatch.setenv('XIANYU_MANAGER_ORDER_CUTOFF_AT', '2026-09-07T00:00:00Z')
    assert RuntimePolicy(**read_runtime_options()).order_cutoff_at == '2026-09-07T00:00:00Z'


def test_final_send_without_payment_evidence_cannot_bypass_cutoff(db):
    service, account = make_service(db, RuntimePolicy(order_cutoff_at='2020-01-01T00:00:00Z'))
    with pytest.raises(OrderCutoffBlocked):
        asyncio.run(service._guarded_send_text(object(), account_id=account, kind='delivery', reference='order:order', chat_id='chat', buyer_id='998877', seller_id='111111', text='synthetic'))


@pytest.mark.parametrize('case', ['good', 'empty', 'wrong_item', 'wrong_account'])
def test_reply_only_exact_knowledge_no_share_requirement(db, monkeypatch, case):
    from xianyu_manager.auto_reply import ReplyDecision
    from test_auto_reply import plain_chat_event
    from xianyu_manager.delivery import extract_plain_chat_message
    service, account = make_service(db, RuntimePolicy(reply_only=True))
    item = '1068148111818'
    db.update_product('01-test', {'listing_url': f'https://www.goofish.com/item?id={item}', 'share_code': 'changed-unverified'}, account)
    with db.connect() as c:
        c.execute("UPDATE products SET knowledge_text=? WHERE dir_name='01-test'", ('' if case == 'empty' else '仅合成知识：支持本地分镜整理。',))
    db.update_auto_reply_settings(account, {'enabled': True})
    generate = AsyncMock(return_value=ReplyDecision(action='reply', reply='支持本地分镜整理。'))
    monkeypatch.setattr(service.reply_client, 'generate', generate)
    monkeypatch.setattr(service.secret_store, 'load', lambda: 'synthetic-only')
    send = AsyncMock()
    monkeypatch.setattr(service, '_send_text', send)
    monkeypatch.setattr(service, '_outbound_preflight', AsyncMock(return_value={'allowed': True}))
    monkeypatch.setattr('xianyu_manager.delivery.random.uniform', lambda *args: 0)
    if case == 'wrong_account':
        service._account_id = account + 1
    event = plain_chat_event()
    message = extract_plain_chat_message(event, '111111')
    if case == 'wrong_item':
        message['item_id'] = '1068148111819'
    async def run():
        for _ in range(2):
            await service._process_chat_event(service._runtime_websocket, account, '111111', message, event)
            tasks = [p.task for p in service._reply_tasks.values()]
            if tasks:
                await asyncio.gather(*tasks)
    asyncio.run(run())
    assert send.await_count == (1 if case == 'good' else 0)
    assert generate.await_count == (1 if case == 'good' else 0)
    if case == 'good':
        assert generate.call_args.kwargs['product']['knowledge_text'] == '仅合成知识：支持本地分镜整理。'
        assert not generate.call_args.kwargs['product']['share_verified']


def test_reply_only_websocket_ignores_orders_and_old_chat(db, monkeypatch):
    import base64
    import json
    from test_auto_reply import plain_chat_event
    service, account = make_service(db, RuntimePolicy(reply_only=True))
    now = int(time.time() * 1000)
    service._reply_listen_started_ms = now - 100
    fresh = plain_chat_event()
    stale = plain_chat_event()
    stale['1']['5'] = str(now - 200)
    paid = {'3': {'redReminder': '等待卖家发货'}, 'timestamp': now}
    frame = {'body': {'syncPushPackage': {'data': [{'data': base64.b64encode(json.dumps(e).encode()).decode()} for e in (fresh, stale, paid)]}}}
    class Socket:
        async def recv(self):
            service._stop_event.set()
            return json.dumps(frame)
    chat = AsyncMock()
    monkeypatch.setattr(service, '_process_chat_event', chat)
    async def forbidden(*args, **kwargs):
        raise AssertionError('order task created')
    monkeypatch.setattr(service, '_process_paid_event', forbidden)
    async def run():
        await service._listen(Socket(), account, '111111', {})
        if service._event_tasks:
            await asyncio.gather(*service._event_tasks)
    asyncio.run(run())
    assert chat.await_count == 1


def test_reply_only_restart_does_not_choose_delivery_start(db, monkeypatch):
    service, account = make_service(db, RuntimePolicy(reply_only=True))
    db.update_auto_reply_settings(account, {'enabled': True})
    reply = AsyncMock(return_value={})
    monkeypatch.setattr(service, 'start_auto_reply', reply)
    async def forbidden(*args, **kwargs):
        raise AssertionError('delivery startup selected')
    monkeypatch.setattr(service, 'start', forbidden)
    asyncio.run(service.start_if_enabled())
    assert reply.await_count == 1


def test_reply_only_selection_api_and_owner_methods_blocked(db, monkeypatch):
    from fastapi.testclient import TestClient
    from xianyu_manager import app as api
    from xianyu_manager.session import BrowserSessionManager
    policy = RuntimePolicy(reply_only=True)
    monkeypatch.setattr(api, 'runtime_policy', policy)
    client = TestClient(api.app, base_url='http://127.0.0.1:8765')
    for url in ('/api/internal/selection/search', '/api/internal/selection/detail', '/api/selection/runs'):
        assert client.post(url, json={}).status_code == 403
    owner = object.__new__(BrowserSessionManager)
    owner.runtime_policy = policy
    with pytest.raises(RuntimeOperationBlocked):
        asyncio.run(owner.search_listings(1, 'synthetic'))
    with pytest.raises(RuntimeOperationBlocked):
        asyncio.run(owner.collect_listing_detail(1, 'https://invalid.example'))


def test_reply_only_lifespan_does_not_implicitly_restart_listener(db, monkeypatch):
    from fastapi.testclient import TestClient
    from xianyu_manager import app as api
    service, account = make_service(db, RuntimePolicy(reply_only=True))
    monkeypatch.setattr(api, 'runtime_policy', service.runtime_policy)
    monkeypatch.setattr(api, 'database', db)
    monkeypatch.setattr(api, 'refresh_products', lambda: [])
    monkeypatch.setattr(api, 'delivery_service', service)
    async def forbidden(*args, **kwargs):
        raise AssertionError('implicit business startup')
    monkeypatch.setattr(service, 'start_if_enabled', forbidden)
    for _ in range(2):
        with TestClient(api.app, base_url='http://127.0.0.1'):
            assert service._task is None


def test_reply_launcher_requires_explicit_flag_and_bound_policy():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('reply_launcher_test', Path(__file__).parents[1] / 'scripts/run_preparation_service.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    policy = RuntimePolicy(reply_only=True, account_id=2)
    launcher.validate_policy(policy, reply_only=True)
    with pytest.raises(SystemExit):
        launcher.validate_policy(policy)
    for invalid in (RuntimePolicy(account_id=2), RuntimePolicy(reply_only=True), RuntimePolicy(prepare_mode=True, reply_only=True, account_id=2), RuntimePolicy(safe_mode=True, reply_only=True, account_id=2)):
        with pytest.raises(SystemExit):
            launcher.validate_policy(invalid, reply_only=True)


def test_reply_only_selected_inactive_record_does_not_require_db_activation(db, monkeypatch):
    from pathlib import Path
    from xianyu_manager.session import BrowserSessionManager
    service, account = make_service(db, RuntimePolicy())
    policy = RuntimePolicy(reply_only=True, account_id=account)
    monkeypatch.setattr(RuntimePolicy, 'require_egress', lambda self: None)
    service.runtime_policy = policy
    with db.connect() as c:
        c.execute('UPDATE accounts SET is_active=0 WHERE id=?', (account,))
    db.update_auto_reply_settings(account, {'enabled': True})
    service.browser_executable = Path(__file__)
    monkeypatch.setattr(service.secret_store, 'has_secret', lambda: True)
    monkeypatch.setattr(service, '_run', AsyncMock())
    owner = BrowserSessionManager(db.path.parent / 'profiles', Path(__file__), db, runtime_policy=policy)
    sentinel = RuntimeError('reached approved owner, no browser launched')
    monkeypatch.setattr(owner, '_launch_visible_browser', AsyncMock(side_effect=sentinel))
    async def run():
        await service.start_auto_reply(account)
        await service._task
        with pytest.raises(RuntimeError, match='reached approved owner'):
            await owner.ensure_runtime_context(account)
    asyncio.run(run())
    assert db.get_account(account)['is_active'] is False
    assert policy.reply_account_selected(db.get_account(account), account)
    assert not policy.reply_account_selected(db.get_account(account), account + 1)
    assert not RuntimePolicy(reply_only=True).reply_account_selected(db.get_account(account), account)


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'wrong_buyer', 'unpaid'])
def test_platform_payment_lookup_fails_closed(db, monkeypatch, case):
    service, account = make_service(db, RuntimePolicy(order_cutoff_at='2020-01-01T00:00:00Z'))
    row = dict(order_id='order', item_id='123', buyer_id='buyer', order_status='待发货', paid_time='2026-09-07T00:00:00Z')
    if case == 'wrong_buyer':
        row['buyer_id'] = 'different'
    if case == 'unpaid':
        row['order_status'] = '待付款'
    rows = [] if case == 'missing' else [row, row] if case == 'duplicate' else [row]
    monkeypatch.setattr(service, '_fetch_recent_sold_orders', AsyncMock(return_value={'orders': rows}))
    with pytest.raises(OrderCutoffBlocked):
        asyncio.run(service._verified_payment_time(account, 'order', '123', 'buyer', {}))
