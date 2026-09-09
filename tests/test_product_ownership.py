import pytest
from test_product_import import intake, uploaded


def archived_reference(intake):
    imp,a=intake;t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    with imp.db.connect() as c:
        c.execute("UPDATE accounts SET is_archived=1,is_active=0,delivery_enabled=0,binding_status='expired' WHERE id=1")
        c.execute("INSERT INTO account_products(account_id,product_dir_name,enabled,listing_status,listing_url) VALUES(1,'45-demo',1,'draft','')")
    return imp,a


def test_archived_empty_draft_does_not_block_and_is_preserved(intake):
    imp,a=archived_reference(intake)
    with imp.db.connect() as c:before=tuple(c.execute('SELECT * FROM account_products WHERE account_id=1').fetchone())
    t=uploaded(intake);p=imp.preview(a,t,None);imp.confirm(a,t,p['preview_id'],True)
    with imp.db.connect() as c:assert tuple(c.execute('SELECT * FROM account_products WHERE account_id=1').fetchone())==before


@pytest.mark.parametrize('change',[
    "UPDATE accounts SET is_archived=0 WHERE id=1",
    "UPDATE accounts SET is_active=1 WHERE id=1",
    "UPDATE accounts SET binding_status='bound' WHERE id=1",
    "UPDATE accounts SET delivery_enabled=1 WHERE id=1",
    "UPDATE account_products SET listing_status='published' WHERE account_id=1",
    "UPDATE account_products SET listing_url='https://www.goofish.com/item?id=999999999' WHERE account_id=1",
    "INSERT INTO account_listings(account_id,item_id,title,listing_url,matched_product_dir_name,is_active) VALUES(1,'999999999','synthetic','https://www.goofish.com/item?id=999999999','45-demo',0)",
    "INSERT INTO orders(xianyu_order_id,account_id,product_dir_name) VALUES('old',1,'45-demo')",
])
def test_real_or_uncertain_foreign_use_still_blocks(intake,change):
    imp,a=archived_reference(intake)
    with imp.db.connect() as c:
        if 'is_active=1' in change:c.execute('UPDATE accounts SET is_active=0')
        c.execute(change)
    with pytest.raises(ValueError,match='其他账号'):uploaded(intake)


def test_foreign_reference_rechecked_before_commit(intake):
    imp,a=archived_reference(intake);t=uploaded(intake);p=imp.preview(a,t,None)
    with imp.db.connect() as c:c.execute('UPDATE accounts SET is_archived=0 WHERE id=1')
    with pytest.raises(ValueError,match='其他账号'):imp.confirm(a,t,p['preview_id'],True)
