from datetime import datetime, timezone

import pytest
import asyncio
from unittest.mock import AsyncMock
from test_fulfillment_a0 import db
from test_order_cutoff_reply_only import make_service
from xianyu_manager.runtime_policy import RuntimePolicy

from xianyu_manager.order_cutoff import (
    OrderCutoffBlocked, merchant_pay_success_time, require_after_cutoff,
)


def test_merchant_wall_clock_uses_shanghai():
    assert merchant_pay_success_time('2026-09-08 14:46:06') == '2026-09-08T06:46:06+00:00'


@pytest.mark.parametrize('text,allowed', [
    ('2026-09-08 08:51:37', False),
    ('2026-09-08 08:51:38', False),
    ('2026-09-08 08:51:39', True),
])
def test_strict_cutoff_after_source_normalization(text, allowed):
    normalized = merchant_pay_success_time(text)
    if allowed:
        assert require_after_cutoff('2026-09-08T00:51:38Z', normalized).tzinfo == timezone.utc
    else:
        with pytest.raises(OrderCutoffBlocked):
            require_after_cutoff('2026-09-08T00:51:38Z', normalized)


def test_generic_naive_time_still_rejected():
    with pytest.raises(OrderCutoffBlocked):
        require_after_cutoff('2026-09-08T00:51:38Z', '2026-09-08 14:46:06')


@pytest.mark.parametrize('value', [None, '', True, 'bad', '2026-02-30 12:00:00',
                                     '2026-09-08T14:46:06', '2026-09-08 14:46'])
def test_missing_invalid_or_other_naive_format_still_blocked(value):
    with pytest.raises(OrderCutoffBlocked):
        require_after_cutoff('2026-09-08T00:51:38Z', merchant_pay_success_time(value))


def test_explicit_utc_is_not_reinterpreted():
    value = '2026-09-08T06:46:06Z'
    assert merchant_pay_success_time(value) == value
    assert require_after_cutoff('2026-09-08T00:51:38Z', value) == datetime(2026, 9, 8, 6, 46, 6, tzinfo=timezone.utc)


@pytest.mark.parametrize('payment', ['2026-09-08 14:46:06', None])
def test_extractor_normalizes_only_payment_source(db, monkeypatch, payment):
    service, _ = make_service(db, RuntimePolicy())
    service._runtime_user_agent = 'synthetic'
    common = {'orderId': '223344556677889900', 'createTime': '2026-09-08 14:46:06'}
    if payment is not None:
        common['paySuccessTime'] = payment
    monkeypatch.setattr(service, '_post_mtop', AsyncMock(return_value={
        'ret': ['SUCCESS'], 'data': {'module': {'items': [{'commonData': common, 'buyerInfoVO': {}}]}}
    }))
    result = asyncio.run(service._fetch_recent_sold_orders({'_m_h5_tk': 'synthetic_seed'}))
    row = result['orders'][0]
    assert row['create_time'] == common['createTime']
    assert row['paid_time'] == ('2026-09-08T06:46:06+00:00' if payment else None)
