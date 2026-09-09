"""Synthetic archives/DB only. No platform, netdisk, browser or sending IO."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_product_import import intake, uploaded, zipped
from xianyu_manager.product_import import import_metadata
from xianyu_manager.fulfillment_rules import delivery_issues, package_safety_fingerprint


def committed(intake):
    imp, account = intake
    token = uploaded(intake)
    preview = imp.preview(account, token, None)
    imp.confirm(account, token, preview['preview_id'], True)
    imp.db.update_product('45-demo', {'share_url':'https://pan.baidu.com/s/synthetic', 'share_code':'test'})
    return imp, account, token


@pytest.mark.parametrize('old_quality', ['unknown', 'failed'])
def test_import_safety_allows_human_verification_not_automatic(intake, old_quality):
    imp, a, _ = committed(intake)
    with imp.db.connect() as c:
        c.execute('UPDATE products SET quality_status=?,quality_errors_json=?', (old_quality, json.dumps(['必须恰好包含五张图片', '含不必要文件'])))
    p = imp.db.get_product('45-demo')
    assert 'QUALITY_BLOCKED' not in p['delivery_issues']
    assert p['delivery_issues'] and not p['share_verified']
    assert not imp.db.list_live_listings(a)[0]['delivery_ready']
    imp.db.confirm_product_share('45-demo', p['fulfillment_fingerprint'])
    p = imp.db.get_product('45-demo')
    assert p['quality_status'] == old_quality  # No fabricated quality approval.
    assert p['share_verified'] and not p['delivery_issues']
    assert imp.db.list_live_listings(a)[0]['delivery_ready']


@pytest.mark.parametrize('field,value', [('zip_hash','b'*64),('zip_name','changed.zip'),('zip_size',123)])
def test_changed_package_invalidates_receipt(intake, field, value):
    imp, a, _ = committed(intake)
    with imp.db.connect() as c:c.execute(f'UPDATE products SET {field}=?', (value,))
    p = imp.db.get_product('45-demo')
    assert 'DELIVERY_PACKAGE_SAFETY_UNCONFIRMED' in p['delivery_issues']
    with pytest.raises(ValueError, match='SAFETY_UNCONFIRMED'):
        imp.db.confirm_product_share('45-demo', p['fulfillment_fingerprint'])


def test_legacy_unknown_still_blocked_and_client_cannot_write_receipt(intake):
    imp, a, _ = committed(intake)
    with imp.db.connect() as c:c.execute("UPDATE products SET delivery_safety_fingerprint=''")
    p=imp.db.get_product('45-demo')
    imp.db.update_product('45-demo', {'delivery_safety_fingerprint':package_safety_fingerprint(p)})
    assert 'QUALITY_BLOCKED' in imp.db.get_product('45-demo')['delivery_issues']


def test_rescan_same_version_keeps_receipt_and_changed_version_blocks(intake):
    imp,a,_=committed(intake)
    p=imp.db.get_product('45-demo');imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    scan=replace(import_metadata(imp.settings.product_library/'45-demo',p['zip_name']), quality_status='failed',quality_errors=['图片数量不正确'])
    imp.db.sync_products([scan])
    assert not imp.db.get_product('45-demo')['delivery_issues']
    imp.db.sync_products([replace(scan,zip_hash='b'*64)])
    p=imp.db.get_product('45-demo')
    assert not p['share_verified'] and not p['verified_fingerprint']
    assert 'DELIVERY_PACKAGE_SAFETY_UNCONFIRMED' in p['delivery_issues']


@pytest.mark.parametrize('change', ['none','disk','symlink','missing'])
def test_api_checks_real_zip_before_human_confirmation(intake,monkeypatch,change):
    from xianyu_manager import app as api
    imp,a,_=committed(intake)
    monkeypatch.setattr(api,'database',imp.db)
    monkeypatch.setattr(api,'settings',SimpleNamespace(product_library=imp.settings.product_library))
    package=imp.settings.product_library/'45-demo/delivery.zip'
    if change=='disk':package.write_bytes(zipped('different.txt'))
    elif change=='missing':package.unlink()
    elif change=='symlink':
        package.rename(package.with_suffix('.saved'));package.symlink_to(package.with_suffix('.saved'))
    p=imp.db.get_product('45-demo')
    c=TestClient(api.app,base_url='http://127.0.0.1:8765') # No lifespan.
    r=c.post('/api/products/45-demo/verify-share',json={'fingerprint':p['fulfillment_fingerprint']})
    assert r.status_code==(200 if change=='none' else 409),r.text
    assert bool(imp.db.get_product('45-demo')['share_verified'])==(change=='none')


@pytest.mark.parametrize('change', ['none','disk','blob','phase','mapping'])
def test_existing_import_recheck_requires_committed_unchanged_evidence(intake,change):
    imp,a,t=committed(intake)
    with imp.db.connect() as c:c.execute("UPDATE products SET delivery_safety_fingerprint=''")
    directory,job=imp._job(a,t)
    if change=='disk':(imp.settings.product_library/'45-demo/extra.txt').write_text('changed')
    elif change=='blob':(directory/f"{job['zip_index']}.blob").write_bytes(b'changed')
    elif change=='phase':job['phase']='needs_manual_recovery';imp._save(directory,job)
    elif change=='mapping':
        with imp.db.connect() as c:c.execute("UPDATE account_listings SET is_active=0")
    with imp.db.connect() as c:
        before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in ['accounts','account_products','account_listings','orders','automation_outbound_events']}
        original=dict(c.execute('SELECT * FROM products').fetchone())
    if change=='none':assert imp.renew_committed_package_safety(a,t)['safety_checked']
    else:
        with pytest.raises(ValueError):imp.renew_committed_package_safety(a,t)
    with imp.db.connect() as c:
        for table,rows in before.items():assert [tuple(r) for r in c.execute('SELECT * FROM '+table)]==rows
        current=dict(c.execute('SELECT * FROM products').fetchone())
    assert {k for k in current if current[k]!=original[k]} <= {'delivery_safety_fingerprint'}
    assert not current['share_verified'] and not current['verified_fingerprint']


def test_receipt_cannot_bypass_claim_or_manual_verification(intake):
    imp,a,_=committed(intake)
    with imp.db.connect() as c:c.execute("UPDATE accounts SET binding_status='bound',delivery_enabled=1 WHERE id=?",(a,))
    imp.db.upsert_paid_order(order_id='synthetic',account_id=a,product_dir_name='45-demo',listing_item_id='123456789',buyer_id='fake',chat_id='fake',event_fingerprint='fake')
    assert imp.db.claim_verified_delivery('synthetic',a,'123456789') is None
    p=imp.db.get_product('45-demo');imp.db.confirm_product_share('45-demo',p['fulfillment_fingerprint'])
    claim=imp.db.claim_verified_delivery('synthetic',a,'123456789')
    assert claim and imp.db.claim_verified_delivery('synthetic',a,'123456789') is None
    assert imp.db.validate_delivery_claim('synthetic',a,'fake',claim['message'])
    with imp.db.connect() as c:c.execute("UPDATE products SET delivery_safety_fingerprint='invalid'")
    assert not imp.db.validate_delivery_claim('synthetic',a,'fake',claim['message'])


@pytest.mark.parametrize('mode', ['safe_mode','prepare_mode','wrong_account'])
def test_maintenance_respects_runtime_boundary(intake,mode):
    imp,a,t=committed(intake)
    if mode=='wrong_account':imp.db.runtime_account_id=a+1
    else:setattr(imp.db,mode,True)
    with pytest.raises(ValueError):imp.renew_committed_package_safety(a,t)


@pytest.mark.parametrize('value', [b'not a ZIP', b''])
def test_invalid_zip_never_gets_safety_receipt(intake,value):
    imp,a=intake
    t=uploaded(intake,{'45-demo/客户交付/delivery.zip':value})
    with pytest.raises(ValueError):imp.preview(a,t,None)
    assert imp.db.get_product('45-demo') is None


def test_database_reopen_retains_receipt_but_not_auto_verification(intake):
    from xianyu_manager.database import Database
    imp,a,_=committed(intake)
    receipt=imp.db.get_product('45-demo')['delivery_safety_fingerprint']
    reopened=Database(imp.db.path).get_product('45-demo',a)
    assert reopened['delivery_safety_fingerprint']==receipt
    assert 'QUALITY_BLOCKED' not in reopened['delivery_issues']
    assert not reopened['share_verified'] and reopened['delivery_issues']
