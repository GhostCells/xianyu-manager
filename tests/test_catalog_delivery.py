import json
from dataclasses import replace
import pytest
from test_fulfillment_a0 import db, setup_order
from xianyu_manager.fulfillment_rules import delivery_issues
from xianyu_manager.runtime_policy import RuntimePolicy, RuntimeOperationBlocked


def prepare(db,monkeypatch):
    a=setup_order(db)
    with db.connect() as c:
        c.execute("UPDATE products SET share_verified=0,share_needs_review=1,verified_fingerprint='' WHERE dir_name='01-test'")
        c.execute("INSERT INTO account_listings(account_id,item_id,title,listing_url,source_kind,source_text,matched_product_dir_name,is_active) VALUES(?, '123','test','https://www.goofish.com/item?id=123','platform_inventory','{\"itemStatus\":0}','01-test',1) ON CONFLICT(account_id,item_id) DO UPDATE SET source_kind='platform_inventory',source_text=excluded.source_text,is_active=1,matched_product_dir_name='01-test'",(a,))
    monkeypatch.setenv('XIANYU_MANAGER_CATALOG_DELIVERY','true')
    return a


def test_catalog_uses_claim_without_forging_verification(db,monkeypatch):
    a=prepare(db,monkeypatch)
    assert not delivery_issues(db.get_product('01-test'))
    p=db.claim_verified_delivery('order',a,'123');assert p
    assert db.claim_verified_delivery('order',a,'123') is None
    assert db.validate_delivery_claim('order',a,'chat',p['message'],buyer_id='buyer')
    assert not db.get_product('01-test')['verified_fingerprint']
    with pytest.raises(ValueError,match='DELIVERY_IN_FLIGHT'):
        db.update_product('01-test',{'share_url':'https://pan.baidu.com/s/new'},a)
    # Simulate an out-of-band change: final claim validation still catches it.
    with db.connect() as c:c.execute("UPDATE products SET share_url='https://pan.baidu.com/s/new'")
    assert not db.validate_delivery_claim('order',a,'chat',p['message'],buyer_id='buyer')


@pytest.mark.parametrize('status,active,source',[(1,1,'platform_inventory'),(-9,1,'platform_inventory'),(0,0,'platform_inventory'),(None,1,'platform_inventory'),(0,1,'manual')])
def test_catalog_requires_current_normal_listing(db,monkeypatch,status,active,source):
    a=prepare(db,monkeypatch)
    with db.connect() as c:c.execute('UPDATE account_listings SET source_text=?,is_active=?,source_kind=?',(json.dumps({'itemStatus':status}),active,source))
    assert db.claim_verified_delivery('order',a,'123') is None


@pytest.mark.parametrize('field,value',[('share_url',''),('share_url','https://invalid.example/s/x'),('zip_hash',''),('zip_size',0),('delivery_kind','invalid')])
def test_incomplete_data_stays_blocked(db,monkeypatch,field,value):
    a=prepare(db,monkeypatch)
    with db.connect() as c:c.execute(f'UPDATE products SET {field}=?',(value,))
    assert db.claim_verified_delivery('order',a,'123') is None


def test_downlisting_invalidates_existing_claim(db,monkeypatch):
    a=prepare(db,monkeypatch);p=db.claim_verified_delivery('order',a,'123');assert p
    with db.connect() as c:c.execute('UPDATE account_listings SET is_active=0')
    assert not db.validate_delivery_claim('order',a,'chat',p['message'],buyer_id='buyer')


def test_catalog_no_item_allowlist_but_cutoff_and_recovery_unchanged():
    p=RuntimePolicy(mvp_fulfillment=True,catalog_delivery=True,order_cutoff_at='2026-09-08T00:00:00Z')
    p.require_delivery_item('999999999999');p.require_fulfillment()
    assert not p.order_recovery_enabled
    with pytest.raises(RuntimeOperationBlocked):p.require_selection()
    with pytest.raises(RuntimeOperationBlocked):replace(p,reply_only=True).require_fulfillment()
    with pytest.raises(ValueError):replace(p,order_cutoff_at='').require_fulfillment()


def test_launcher_accepts_catalog_without_allowlist():
    import runpy
    launcher=runpy.run_path('scripts/run_preparation_service.py')
    p=RuntimePolicy(account_id=2,mvp_fulfillment=True,catalog_delivery=True,order_cutoff_at='2026-09-08T00:00:00Z')
    launcher['validate_policy'](p,mvp=True)
    with pytest.raises(SystemExit):launcher['validate_policy'](replace(p,catalog_delivery=False),mvp=True)
    with pytest.raises(ValueError):launcher['validate_policy'](replace(p,order_cutoff_at=''),mvp=True)


def test_config_allows_empty_allowlist_only_in_explicit_catalog_mode(monkeypatch):
    from xianyu_manager.config import read_runtime_options
    for key,value in {'MVP_FULFILLMENT':'true','CATALOG_DELIVERY':'true','RESIDENT_REPLY':'true','REPLY_ONLY':'false','PREPARE_MODE':'false','ACCOUNT_ID':'2','LOGIN_AUTHORIZED':'true','FULFILLMENT_ITEMS':'','ORDER_CUTOFF_AT':'2026-09-08T00:00:00Z'}.items():
        monkeypatch.setenv('XIANYU_MANAGER_'+key,value)
    assert read_runtime_options()['catalog_delivery']
    monkeypatch.setenv('XIANYU_MANAGER_CATALOG_DELIVERY','false')
    with pytest.raises(ValueError):read_runtime_options()
