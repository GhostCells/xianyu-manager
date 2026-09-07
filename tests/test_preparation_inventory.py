"""Synthetic-only manual inventory window tests; never launch a browser."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from xianyu_manager.database import Database
from xianyu_manager.preparation_inventory import collect_once, match_items, diagnose_cards


def card(i, **fields):
    return {'cardData': {'id': str(123456780 + i), 'title': 'synthetic', 'itemStatus': 0, **fields}}


def test_diagnostic_exclusions_and_duplicates():
    cards = [card(0), card(0), card(1), card(2, itemStatus=1),
             card(3, id='invalid'), card(4, title=''), {'cardData': None}, None]
    d = diagnose_cards(cards, 2, {'123456781'})
    for key in ('status_excluded', 'invalid_id', 'empty_title', 'invalid_card_data',
                'invalid_card', 'same_page_duplicates', 'cross_page_duplicates'):
        assert d[key] == 1
    assert d['raw_cards'] == 8 and d['valid_before_dedup'] == 3
    assert d['unique_cumulative'] == 2
    assert len(d['excluded']) == 2


def test_raw_34_filtered_31_with_separate_completeness(owner):
    obj, response = owner
    response.json.return_value['data']['cardList'] = [card(i) for i in range(31)] + [
        card(31, itemStatus=1), card(32, itemStatus=2), card(33, title='')]
    report = asyncio.run(collect_once(obj, 2, 34))
    assert report['pagination_complete'] and not report['frontend_count_reconciled']
    assert report['totals']['raw_cards'] == 34
    assert report['totals']['unique_ids'] == 31
    assert report['totals']['status_excluded'] == 2
    assert report['totals']['empty_title'] == 1


def test_diagnostic_cross_page_and_cursor_redaction(owner):
    obj, response = owner
    response.json.side_effect = [
        {'ret': ['SUCCESS::ok'], 'data': {'cardList': [card(0)], 'nextPage': True,
            'nextPageModel': {'private': 'must-not-persist'}, 'nextPageNum': 2}},
        {'ret': ['SUCCESS::ok'], 'data': {'cardList': [card(0), card(1)], 'nextPage': False}}]
    report = asyncio.run(collect_once(obj, 2, 2))
    assert report['pages'] == 2 and report['frontend_count_reconciled']
    assert report['totals']['cross_page_duplicates'] == 1
    assert 'must-not-persist' not in json.dumps(report)


def test_missing_next_page_not_complete(owner):
    obj, response = owner
    del response.json.return_value['data']['nextPage']
    report = asyncio.run(collect_once(obj, 2, 1))
    assert not report['pagination_complete'] and not report['frontend_count_reconciled']
from xianyu_manager.runtime_policy import RuntimeOperationBlocked, RuntimePolicy
from xianyu_manager.session import BrowserSessionManager


def test_exact_mapping_conflict_and_listing_only():
    items = [{'item_id': '123456789', 'title': 'same title'}]
    product = {'dir_name': 'p', 'title': 'same title', 'catalog_status': 'listing_only'}
    assert match_items(items, [product], [])[0]['issues'] == ['MAPPING_MISSING']
    product['listing_url'] = 'https://www.goofish.com/item?id=1234567890'
    assert match_items(items, [product], [])[0]['issues'] == ['MAPPING_MISSING']
    product['listing_url'] = 'https://www.goofish.com/item?id=123456789'
    assert 'LISTING_ONLY' in match_items(items, [product], [])[0]['issues']
    listing = {'item_id': '123456789', 'matched_product_dir_name': 'other'}
    assert match_items(items, [product], [listing])[0]['issues'] == ['MAPPING_CONFLICT']


@pytest.fixture
def owner(tmp_path, monkeypatch):
    db = Database(tmp_path / 'manager.db', prepare_mode=True)
    policy = SimpleNamespace(require_account=lambda a: None,
                             preparation_permission=lambda action: 'synthetic-approval')
    response = SimpleNamespace(status=200, dispose=AsyncMock(), json=AsyncMock(return_value={
        'ret': ['SUCCESS::ok'], 'data': {'cardList': [{'cardData': {
            'id': '123456789', 'title': 'synthetic', 'itemStatus': 0}}], 'nextPage': False}}))
    context = SimpleNamespace(cookies=AsyncMock(return_value=[
        {'name': '_m_h5_tk', 'value': 'synthetic_token'}, {'name': 'unb', 'value': 'synthetic'}]),
        request=SimpleNamespace(post=AsyncMock(return_value=response)))
    monkeypatch.setattr('xianyu_manager.preparation_inventory.asyncio.sleep', AsyncMock())
    return SimpleNamespace(database=db, runtime_policy=policy, _context=context), response


def test_once_scoped_complete_and_no_business_data_changes(owner):
    obj, response = owner
    with obj.database.connect() as c:
        before = list(c.iterdump())
    report = asyncio.run(collect_once(obj, 2))
    assert report['complete'] and report['pages'] == 1
    assert report['items'][0]['item_id'] == '123456789'
    assert not report['items'][0]['business_allowed']
    assert 'synthetic_token' not in json.dumps(report)
    with obj.database.connect() as c:
        assert list(c.iterdump()) == before
    with pytest.raises(RuntimeOperationBlocked, match='ALREADY_CONSUMED'):
        asyncio.run(collect_once(obj, 2))
    assert obj._context.request.post.await_count == 1
    assert response.dispose.await_count == 1


@pytest.mark.parametrize('next_page,expected_pages', [(True, 5), (None, 1), ('false', 1)])
def test_bounded_pages_and_unknown_completeness(owner, next_page, expected_pages):
    obj, response = owner
    payload = response.json.return_value
    payload['data']['nextPage'] = next_page
    report = asyncio.run(collect_once(obj, 2))
    assert not report['complete'] and report['pages'] == expected_pages
    assert obj._context.request.post.await_count == expected_pages


def test_verification_stops_without_retry_or_error_disclosure(owner):
    obj, response = owner
    response.json.return_value = {'ret': ['FAIL_SYS_USER_VALIDATE::private-challenge-url']}
    report = asyncio.run(collect_once(obj, 2))
    assert report['status'] == 'stopped_requires_operator_review'
    assert not report['complete'] and not report['items']
    assert 'private-challenge-url' not in json.dumps(report)
    assert obj._context.request.post.await_count == 1


def test_permission_checked_before_any_io(owner):
    obj, _ = owner
    def forbidden(action):
        raise RuntimeOperationBlocked('no permit')
    obj.runtime_policy.preparation_permission = forbidden
    with pytest.raises(RuntimeOperationBlocked):
        asyncio.run(collect_once(obj, 2))
    assert not obj._context.cookies.called


def test_owner_requires_live_manual_confirmation(owner):
    obj, _ = owner
    obj.runtime_policy.require_login = lambda account: None
    obj._account_id, obj._handoff_account_id, obj._lock = 2, None, asyncio.Lock()
    with pytest.raises(RuntimeOperationBlocked, match='MANUAL_LOGIN_CONFIRMATION_REQUIRED'):
        asyncio.run(BrowserSessionManager.read_preparation_inventory(obj, 2))
    assert not obj._context.cookies.called


@pytest.mark.parametrize('operations', [None, 'login', [], ['inventory_once']])
def test_manual_capability_not_satisfied_by_generic_egress(monkeypatch, operations):
    import xianyu_manager.runtime_policy as module
    monkeypatch.setattr(RuntimePolicy, 'require_egress', lambda self: None)
    monkeypatch.setattr(module, 'clock_valid', lambda data: True)
    monkeypatch.setattr(module, 'read_root_json', lambda p: {
        'purpose': 'manual_login_inventory', 'account_id': 2,
        'approval_id': 'synthetic', 'operations': operations})
    policy = RuntimePolicy(prepare_mode=True, account_id=2, login_authorized=True)
    with pytest.raises(RuntimeOperationBlocked):
        policy.require_login(2)


def test_prepare_always_forbids_business_even_manual_permission():
    with pytest.raises(RuntimeOperationBlocked, match='PREPARE_BUSINESS_FORBIDDEN'):
        RuntimePolicy(prepare_mode=True, account_id=2, login_authorized=True).require_business()
