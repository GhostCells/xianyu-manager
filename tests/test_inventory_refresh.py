import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_fulfillment_a0 import db
from xianyu_manager.inventory_refresh import InventoryObservation, store, status
from xianyu_manager.delivery import DeliveryService
from xianyu_manager.runtime_policy import RuntimePolicy
from xianyu_manager.security import SecretStore


def card(i=123456789, s=0):
    return {'cardData': {'id':str(i), 'title':'synthetic', 'itemStatus':s}}


def report(cards):
    o = InventoryObservation()
    o.consume({'cardList':cards, 'nextPage':False}, 1)
    return o.result()


def test_counts_special_duplicates():
    o = InventoryObservation()
    o.consume({'cardList':[card(),card()], 'nextPage':True},1)
    o.consume({'cardList':[card(),card(987654321,-9)], 'nextPage':False},2)
    r=o.result()
    assert (r['raw_count'],r['normal_count'],r['special_count'],r['unique_count'])==(4,1,1,2)


@pytest.mark.parametrize('data', [None,{}, {'cardList':[]}, {'cardList':[], 'nextPage':0},
    {'cardList':[], 'nextPage':True}, {'cardList':[{}], 'nextPage':False},
    {'cardList':[card(s=None)], 'nextPage':False},
    {'cardList':[card(i='bad')], 'nextPage':False},
    {'cardList':[{'cardData':{'id':'123456789','itemStatus':0,'title':''}}], 'nextPage':False}])
def test_invalid_payload_rejected(data):
    with pytest.raises(ValueError): InventoryObservation().consume(data,1)


def test_conflicting_duplicate_rejected():
    o=InventoryObservation()
    with pytest.raises(ValueError):o.consume({'cardList':[card(),card(s=-9)],'nextPage':False},1)


def test_page_limit_incomplete():
    o=InventoryObservation()
    for i in range(5):o.consume({'cardList':[card()],'nextPage':True},i+1)
    with pytest.raises(ValueError,match='INCOMPLETE'):o.result()


def test_store_preserves_products_bindings_history(db):
    a=db.get_active_account()['id']
    with db.connect() as c:
        c.execute("INSERT INTO account_listings(account_id,item_id,title,listing_url,matched_product_dir_name) VALUES(?, '111111111','old','https://www.goofish.com/item?id=111111111','01-test')",(a,))
    with db.connect() as c:
        old={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in ('products','account_products','accounts','orders')}
        c.execute('BEGIN IMMEDIATE')
        store(c,a,report([card(),card(987654321,-9)]))
        for t,rows in old.items():assert [tuple(r) for r in c.execute('SELECT * FROM '+t)]==rows
        historical=c.execute("SELECT * FROM account_listings WHERE item_id='111111111'").fetchone()
        assert not historical['is_active'] and historical['matched_product_dir_name']=='01-test'
        assert c.execute("SELECT is_active FROM account_listings WHERE item_id='987654321'").fetchone()[0]==0
        assert status(c,a)['special_count']==1


def test_empty_complete_and_failed_preservation(db):
    a=db.get_active_account()['id']
    with db.connect() as c:
        store(c,a,report([card()]))
    with db.connect() as c:
        with pytest.raises(ValueError):store(c,a,{'pagination_complete':False})
        assert c.execute('SELECT count(*) FROM account_listings WHERE is_active=1').fetchone()[0]==1
        store(c,a,report([]))
        assert c.execute('SELECT count(*) FROM account_listings WHERE is_active=1').fetchone()[0]==0


@pytest.fixture
def service(db,tmp_path,monkeypatch):
    a=db.get_active_account()['id']
    monkeypatch.setattr(RuntimePolicy,'require_egress',lambda self:None)
    s=DeliveryService(tmp_path/'profile',None,db,SecretStore(tmp_path/'secret'),runtime_policy=RuntimePolicy(account_id=a))
    s._account_id=a;s._status='listening';s._runtime_seller_id='12345';s._runtime_cookie_map={'_m_h5_tk':'fake_seed'};s._runtime_websocket=object()
    return s


def test_service_never_scans_or_reconciles(service,monkeypatch):
    def forbidden(*a,**kw):raise AssertionError('unexpected mutation')
    monkeypatch.setattr(service.database,'reconcile_configured_listings',forbidden)
    monkeypatch.setattr(service.database,'sync_live_listings',forbidden)
    monkeypatch.setattr(service,'_fetch_live_inventory',AsyncMock(return_value=report([card()])))
    assert asyncio.run(service.refresh_inventory_only())['normal_count']==1
    with pytest.raises(ValueError,match='COOLDOWN'):asyncio.run(service.refresh_inventory_only())


def test_service_error_leaves_snapshot(service,monkeypatch):
    monkeypatch.setattr(service,'_fetch_live_inventory',AsyncMock(side_effect=ValueError('INVENTORY_INCOMPLETE')))
    with pytest.raises(ValueError):asyncio.run(service.refresh_inventory_only())
    with service.database.connect() as c:assert status(c,service._account_id)['needs_refresh']


def test_strict_fetch(service,monkeypatch):
    fetch=AsyncMock(side_effect=[{'ret':['SUCCESS::ok'],'data':{'cardList':[card()],'nextPage':True,'nextPageNum':2}},
                               {'ret':['SUCCESS::ok'],'data':{'cardList':[card(987654321,-9)],'nextPage':False}}])
    monkeypatch.setattr(service,'_post_mtop',fetch)
    monkeypatch.setattr('xianyu_manager.delivery.asyncio.sleep',AsyncMock())
    r=asyncio.run(service._fetch_live_inventory(service._runtime_cookie_map,'12345',strict_observation=True))
    assert r['raw_count']==2 and r['special_count']==1
    assert json.loads(fetch.call_args_list[1].kwargs['data_json'])['nextPageNum']==2


def test_session_change_prevents_save(service,monkeypatch):
    async def changed(*a,**kw):
        service._runtime_websocket=object()
        return report([card()])
    monkeypatch.setattr(service,'_fetch_live_inventory',changed)
    with pytest.raises(ValueError,match='SESSION_CHANGED'):asyncio.run(service.refresh_inventory_only())


def test_failure_metadata_retains_last_success(db):
    from xianyu_manager.inventory_refresh import mark_failed
    a=db.get_active_account()['id']
    with db.connect() as c:
        first=store(c,a,report([card()]))
        mark_failed(c,a,'INVENTORY_INCOMPLETE_KEEP_PREVIOUS')
        current=status(c,a)
        assert current['last_success_at']==first['last_success_at']
        assert current['needs_refresh'] and current['last_error'].startswith('INVENTORY_')
        assert c.execute('SELECT count(*) FROM account_listings WHERE is_active=1').fetchone()[0]==1


@pytest.mark.parametrize('policy',[RuntimePolicy(True),RuntimePolicy(prepare_mode=True)])
def test_safe_and_prepare_never_fetch(service,monkeypatch,policy):
    service.runtime_policy=policy
    fetch=AsyncMock()
    monkeypatch.setattr(service,'_fetch_live_inventory',fetch)
    with pytest.raises(RuntimeError):asyncio.run(service.refresh_inventory_only())
    fetch.assert_not_called()
