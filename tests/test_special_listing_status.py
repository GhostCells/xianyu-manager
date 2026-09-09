import pytest
from xianyu_manager.listing_status import sellable_status
from test_catalog_delivery import db, prepare


def test_special_mapping_keeps_data_and_claim_guards(monkeypatch, db):
    a = prepare(db, monkeypatch)
    iid = '1074719040968'
    monkeypatch.setenv('XIANYU_MANAGER_SPECIAL_STATUS_ITEMS', iid)
    with db.connect() as c:
        c.execute('UPDATE account_products SET listing_url=? WHERE account_id=?', ('https://www.goofish.com/item?id='+iid, a))
        c.execute('UPDATE account_listings SET item_id=?,source_text=?', (iid, '{"itemStatus":-9}'))
        assert db._mapped_product(c, a, iid)
        c.execute("UPDATE products SET zip_hash=''")
    assert db.claim_verified_delivery('order', a, iid) is None
    with db.connect() as c:
        c.execute('UPDATE products SET zip_hash=?', ('a'*64,))
        c.execute('UPDATE account_listings SET is_active=0')
        assert db._mapped_product(c, a, iid) is None


def test_exact_account_status_and_id_only(monkeypatch):
    monkeypatch.setenv('XIANYU_MANAGER_SPECIAL_STATUS_ITEMS','1075865170356,1072681555598,1074719040968')
    for iid in ['1075865170356','1072681555598','1074719040968']:
        assert sellable_status(2,iid,-9)
        assert not sellable_status(1,iid,-9)
        assert not sellable_status(2,iid,1)
    assert not sellable_status(2,'999999999999',-9)
    assert sellable_status(2,'999999999999',0)


def test_malformed_config_fails_closed(monkeypatch):
    monkeypatch.setenv('XIANYU_MANAGER_SPECIAL_STATUS_ITEMS','1075865170356,*')
    assert not sellable_status(2,'1075865170356',-9)


def test_refresh_keeps_raw_status_and_deactivates_missing(monkeypatch,db):
    from xianyu_manager.inventory_refresh import store
    monkeypatch.setenv('XIANYU_MANAGER_SPECIAL_STATUS_ITEMS','1075865170356')
    a=db.get_active_account()['id']
    report={'pagination_complete':True,'items':[{'item_id':'1075865170356','title':'synthetic','item_status':-9},{'item_id':'999999999999','title':'other','item_status':-9}],'observed_at':'synthetic'}
    with db.connect() as c:
        store(c,a,report)
        rows=c.execute('SELECT item_id,is_active,source_text FROM account_listings').fetchall()
        assert [(r['item_id'],r['is_active']) for r in rows]==[('1075865170356',1),('999999999999',0)]
        assert '-9' in rows[0]['source_text']
        store(c,a,{**report,'items':[]})
        assert not c.execute('SELECT 1 FROM account_listings WHERE is_active=1').fetchone()
