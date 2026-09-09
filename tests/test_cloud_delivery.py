"""No real network, platform, cloud drive or outgoing messages."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_product_import import intake, uploaded
from xianyu_manager.fulfillment_rules import delivery_issues
from xianyu_manager.product_import import import_metadata


def cloud_upload(intake, text='教程商品使用说明，仅供合成测试。'):
    imp, a = intake
    data = text.encode()
    job = imp.start(a, '123456789', '45-demo', [{'path':'45-demo/商品资料/说明.txt','size':len(data)}])
    async def chunks():yield data
    asyncio.run(imp.upload(a,job['import_id'],0,chunks()))
    return job['import_id']


def cloud_commit(intake):
    imp,a=intake;token=cloud_upload(intake)
    p=imp.preview(a,token,None,'cloud');imp.confirm(a,token,p['preview_id'],True)
    imp.db.update_product('45-demo',{'share_url':'https://pan.baidu.com/s/synthetic','share_code':'test'})
    return imp,a,token


def test_no_zip_requires_explicit_cloud_choice(intake):
    imp,a=intake;t=cloud_upload(intake)
    p=imp.preview(a,t,None)
    assert p['requires_zip_selection'] and not p['packages'] and '没有交付ZIP' in p['message']
    assert 'preview_id' not in p
    p=imp.preview(a,t,None,'cloud')
    assert p['delivery_kind']=='cloud' and not p['zip_name'] and not p['zip_hash'] and p['zip_size']==0
    assert len(p['delivery_revision'])==64
    imp.confirm(a,t,p['preview_id'],True)
    product=imp.db.get_product('45-demo')
    assert product['delivery_kind']=='cloud' and product['quality_status']=='unknown'
    assert not product['share_verified'] and not product['verified_fingerprint']
    assert 'SHARE_UNVERIFIED' in delivery_issues(product)
    assert not any(code in delivery_issues(product) for code in ['QUALITY_BLOCKED','DELIVERY_PACKAGE_UNCONFIRMED'])


def test_cloud_still_needs_current_human_verification(intake):
    imp,a,t=cloud_commit(intake);p=imp.db.get_product('45-demo')
    assert not imp.db.list_live_listings(a)[0]['delivery_ready']
    imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    assert imp.db.list_live_listings(a)[0]['delivery_ready']
    assert not imp.db.get_product('45-demo')['delivery_issues']
    assert not list(imp.settings.product_library.rglob('*.zip'))


@pytest.mark.parametrize('field,value',[('share_url','https://pan.baidu.com/s/changed'),('share_code','new'),('delivery_revision','b'*64),('delivery_kind','zip')])
def test_cloud_changes_cannot_reuse_old_fingerprint(intake,field,value):
    imp,a,_=cloud_commit(intake);p=imp.db.get_product('45-demo')
    imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    with imp.db.connect() as c:c.execute(f'UPDATE products SET {field}=?',(value,))
    assert 'VERIFICATION_VERSION_UNCONFIRMED' in imp.db.get_product('45-demo')['delivery_issues']


def test_cloud_reimport_revokes_verification_and_keeps_old_assets(intake):
    imp,a,_=cloud_commit(intake);p=imp.db.get_product('45-demo');imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    t=cloud_upload(intake,'新版教程资料');preview=imp.preview(a,t,None,'cloud');imp.confirm(a,t,preview['preview_id'],True)
    current=imp.db.get_product('45-demo')
    assert not current['share_verified'] and current['share_needs_review']
    assert current['delivery_revision']!=p['delivery_revision']
    assert (imp.root/t/'previous-product/商品资料/说明.txt').is_file()


@pytest.mark.parametrize('change',['none','missing','disk','symlink'])
def test_cloud_api_checks_local_assets_without_opening_netdisk(intake,monkeypatch,change):
    from xianyu_manager import app as api
    imp,a,_=cloud_commit(intake)
    monkeypatch.setattr(api,'database',imp.db);monkeypatch.setattr(api,'settings',SimpleNamespace(product_library=imp.settings.product_library))
    p=imp.db.get_product('45-demo');folder=imp.settings.product_library/'45-demo';f=folder/'商品资料/说明.txt'
    if change=='missing':f.unlink()
    elif change=='disk':f.write_text('changed')
    elif change=='symlink':f.unlink();f.symlink_to(folder/'发布文案.txt')
    response=TestClient(api.app,base_url='http://127.0.0.1:8765').post('/api/products/45-demo/verify-share',json={'fingerprint':p['fulfillment_fingerprint']})
    assert response.status_code==(200 if change=='none' else 409)


def test_cloud_fields_cannot_be_written_through_generic_product_update(intake):
    imp,a,_=cloud_commit(intake);p=imp.db.get_product('45-demo')
    imp.db.update_product('45-demo',{'delivery_kind':'zip','delivery_revision':'b'*64})
    after=imp.db.get_product('45-demo')
    assert after['delivery_kind']==p['delivery_kind'] and after['delivery_revision']==p['delivery_revision']


def test_scan_does_not_require_a_fake_zip_for_cloud(intake):
    imp,a,_=cloud_commit(intake);p=imp.db.get_product('45-demo');imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    imp.db.sync_products([replace(import_metadata(imp.settings.product_library/'45-demo',''),quality_status='failed',quality_errors=['没有ZIP','没有五张图片'])])
    assert not imp.db.get_product('45-demo')['delivery_issues']


@pytest.mark.parametrize('kind,index',[('unknown',None),('cloud',0),('',None)])
def test_unknown_or_ambiguous_delivery_kind_rejected(intake,kind,index):
    imp,a=intake;t=cloud_upload(intake)
    with pytest.raises(ValueError):imp.preview(a,t,index,kind)


def test_zip_cloud_switch_revokes_old_human_verification(intake):
    imp,a,_=cloud_commit(intake);p=imp.db.get_product('45-demo');imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    t=uploaded(intake);preview=imp.preview(a,t,None);imp.confirm(a,t,preview['preview_id'],True)
    p=imp.db.get_product('45-demo')
    assert p['delivery_kind']=='zip' and not p['delivery_revision'] and p['delivery_safety_fingerprint']
    assert not p['share_verified']


def test_cloud_uses_same_claim_and_prevents_duplicates(intake):
    imp,a,_=cloud_commit(intake)
    with imp.db.connect() as c:
        c.execute("UPDATE accounts SET binding_status='bound',delivery_enabled=1 WHERE id=?",(a,))
        c.execute("INSERT INTO orders(xianyu_order_id,account_id,product_dir_name,listing_item_id,payment_status,delivery_status,chat_id) VALUES('synthetic',?,'45-demo','123456789','paid','pending','chat')",(a,))
    assert imp.db.claim_verified_delivery('synthetic',a,'123456789') is None
    p=imp.db.get_product('45-demo');imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    claim=imp.db.claim_verified_delivery('synthetic',a,'123456789')
    assert claim and imp.db.validate_delivery_claim('synthetic',a,'chat',claim['message'])
    assert imp.db.claim_verified_delivery('synthetic',a,'123456789') is None
    with imp.db.connect() as c:c.execute("UPDATE products SET delivery_revision=?",('f'*64,))
    assert not imp.db.validate_delivery_claim('synthetic',a,'chat',claim['message'])
