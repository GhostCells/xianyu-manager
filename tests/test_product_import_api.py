from types import SimpleNamespace
from fastapi.testclient import TestClient
import pytest
from test_product_import import intake, zipped


@pytest.fixture
def client(intake,monkeypatch):
    from xianyu_manager import app as api
    imp,a=intake
    policy=SimpleNamespace(safe_mode=False,reply_only=False,mode='normal',managed=True,account_id=a)
    monkeypatch.setattr(api,'runtime_policy',policy)
    monkeypatch.setattr(api,'database',imp.db)
    monkeypatch.setattr(api,'product_imports',imp)
    return TestClient(api.app,base_url='http://127.0.0.1'),policy


def test_api_preview_confirm_and_no_business_calls(client,intake,monkeypatch):
    from xianyu_manager import app as api
    def forbidden(*a,**k):raise AssertionError('business operation forbidden')
    for name in ('start','start_auto_reply','refresh_live_listings','refresh_inventory_only'):
        monkeypatch.setattr(api.delivery_service,name,forbidden)
    c,_=client
    headers={'X-Product-Import':'confirm-local'}
    files=[('45-demo/商品资料/说明.txt','synthetic knowledge'.encode()),('45-demo/客户交付/a.zip',zipped())]
    r=c.post('/api/product-imports',headers=headers,json={'item_id':'123456789','product_dir':'45-demo','files':[{'path':p,'size':len(b)} for p,b in files]})
    assert r.status_code==200,r.text
    token=r.json()['import_id']
    for i,(_,data) in enumerate(files):
        r=c.put(f'/api/product-imports/{token}/files/{i}',headers=headers,content=data)
        assert r.status_code==200,r.text
    r=c.post(f'/api/product-imports/{token}/preview',headers=headers,json={})
    assert r.status_code==200,r.text
    preview=r.json()['preview_id']
    assert intake[0].db.get_product('45-demo') is None
    r=c.post(f'/api/product-imports/{token}/confirm',headers=headers,json={'preview_id':preview,'accept_replace_and_unverify':True})
    assert r.status_code==200,r.text
    assert r.json()['verified'] is False


@pytest.mark.parametrize('mode',['safe','prepare','wrong_account','missing_header','cross_origin'])
def test_api_authority_boundaries(client,mode):
    c,policy=client
    headers={'X-Product-Import':'confirm-local'}
    if mode=='safe':policy.safe_mode=True;policy.error_message='safe'
    if mode=='prepare':policy.mode='prepare'
    if mode=='wrong_account':policy.account_id=999
    if mode=='missing_header':headers={}
    if mode=='cross_origin':headers['Origin']='https://evil.invalid'
    r=c.post('/api/product-imports',headers=headers,json={'item_id':'123456789','product_dir':'45-demo','files':[{'path':'45-demo/a','size':0}]})
    assert r.status_code in {403,409}


def test_manifest_body_limit(client):
    c,_=client
    r=c.post('/api/product-imports',headers={'X-Product-Import':'confirm-local'},content=b' '* (512*1024+1))
    assert r.status_code==413
