import importlib.util
import os
from pathlib import Path
import sqlite3

import pytest

spec=importlib.util.spec_from_file_location('prepare_import_directories',Path(__file__).resolve().parents[1]/'scripts/prepare_import_directories.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


@pytest.fixture
def assets(tmp_path):
    library=(tmp_path/'library');library.mkdir();library=library.resolve()
    db=tmp_path/'test.sqlite'
    c=sqlite3.connect(db)
    c.executescript('''CREATE TABLE products(dir_name TEXT,catalog_status TEXT);
        CREATE TABLE account_products(product_dir_name TEXT,account_id INTEGER,listing_status TEXT DEFAULT 'draft',listing_url TEXT DEFAULT '');
        CREATE TABLE accounts(id INTEGER,is_archived INTEGER,is_active INTEGER,delivery_enabled INTEGER,binding_status TEXT);
        CREATE TABLE account_listings(matched_product_dir_name TEXT,account_id INTEGER);
        CREATE TABLE orders(account_id INTEGER,delivery_status TEXT,product_dir_name TEXT);''')
    c.execute("INSERT INTO accounts VALUES(1,0,0,0,'unbound')")
    for name,status,account in [('01-current','active',2),('02-legacy','legacy',2),('03-other','active',1),('04-shared','active',2),('05-missing','active',2)]:
        c.execute('INSERT INTO products VALUES(?,?)',(name,status));c.execute('INSERT INTO account_products(product_dir_name,account_id) VALUES(?,?)',(name,account))
        if name=='05-missing':continue
        p=library/name;p.mkdir();(p/'original.txt').write_text('unchanged');(p/'original.txt').chmod(0o400);p.chmod(0o500)
    c.execute("INSERT INTO account_listings VALUES('04-shared',1)")
    c.execute("INSERT INTO products VALUES('__listing__123456789','active')")
    c.execute("INSERT INTO account_products(product_dir_name,account_id) VALUES('__listing__123456789',2)")
    c.commit();c.close()
    yield db,library
    for p in library.iterdir():
        if p.is_dir() and not p.is_symlink():p.chmod(0o700)


def test_handoff_is_nonrecursive_and_account_scoped(assets):
    db,library=assets
    plan=module.plan_directories(db,library,2,os.getuid())
    assert [p['name'] for p in plan]==['01-current']
    before=db.read_bytes();file=(library/'01-current/original.txt');st=file.stat()
    module.apply_directories(library,plan)
    assert file.read_text()=='unchanged' and file.stat().st_mode==st.st_mode and file.stat().st_uid==st.st_uid
    assert db.read_bytes()==before
    assert (library/'01-current').stat().st_mode & 0o700==0o700
    assert not module.plan_directories(db,library,2,os.getuid())
    assert (library/'02-legacy').stat().st_mode & 0o777==0o500


def test_handoff_rejects_symlink(assets,tmp_path):
    db,library=assets
    p=library/'01-current';p.chmod(0o700);p.rename(tmp_path/'original');p.symlink_to(tmp_path/'original')
    with pytest.raises(ValueError,match='symlink'):module.plan_directories(db,library,2,os.getuid())


def test_handoff_rejects_changed_inode(assets):
    db,library=assets;plan=module.plan_directories(db,library,2,os.getuid())
    p=library/'01-current';p.rename(library/'old');p.mkdir()
    with pytest.raises(ValueError,match='changed'):module.apply_directories(library,plan)


def test_handoff_waits_for_active_send(assets):
    db,library=assets
    c=sqlite3.connect(db);c.execute("INSERT INTO orders(account_id,delivery_status) VALUES(2,'sending')");c.commit();c.close()
    with pytest.raises(ValueError,match='SEND_IN_PROGRESS'):module.plan_directories(db,library,2,os.getuid())
