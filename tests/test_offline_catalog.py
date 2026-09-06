import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from xianyu_manager.database import Database
from xianyu_manager.fulfillment_rules import compose_delivery_message, matches_registered_listing
from xianyu_manager.offline_catalog import CatalogError, export_catalog, main, read_catalog
from xianyu_manager.scanner import ScannedProduct


@pytest.fixture
def existing_db(tmp_path):
    path = tmp_path / "snapshot.db"
    db = Database(path)
    db.sync_products([ScannedProduct(
        dir_name="01-synthetic", number=1, name="synthetic", title="Synthetic product",
        zip_name="synthetic.zip", zip_hash="a" * 64, zip_size=1, image_count=5,
        quality_status="passed", quality_errors=[], scanned_at="2026-01-01",
    )])
    db.ensure_default_accounts()
    with db.connect() as c:
        c.execute("UPDATE products SET share_url='https://pan.baidu.com/s/synthetic',share_code='FAKE',share_verified=1")
        c.execute("UPDATE account_products SET enabled=1,listing_status='published',listing_url='https://www.goofish.com/item?id=12345678'")
        c.execute("INSERT INTO account_listings(account_id,item_id,title,listing_url,matched_product_dir_name) VALUES(1,'12345678','test','https://www.goofish.com/item?id=12345678','01-synthetic')")
        c.execute("INSERT INTO orders(xianyu_order_id,account_id,product_dir_name,buyer_id) VALUES('test-order',1,'01-synthetic','BUYER_MUST_NOT_EXPORT')")
        c.execute("INSERT INTO chat_messages(account_id,chat_id,direction,content,event_fingerprint) VALUES(1,'chat','in','CHAT_MUST_NOT_EXPORT','event')")
        c.execute("UPDATE products SET knowledge_text='KNOWLEDGE_MUST_NOT_EXPORT'")
    from xianyu_manager.fulfillment_rules import fulfillment_fingerprint
    db.confirm_product_share("01-synthetic", fulfillment_fingerprint(db.get_product("01-synthetic")))
    return path


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_export_preserves_database_and_excludes_unneeded_data(existing_db, tmp_path, monkeypatch):
    before = fingerprint(existing_db)
    # Once fixtures exist, any Database initialization would perform migrations
    # and must fail. The offline tool must never import or call the app runtime.
    monkeypatch.setattr(Database, "__init__", lambda *a, **kw: pytest.fail("migration attempted"))
    output = tmp_path / "private-export"
    summary = export_catalog(existing_db, output)
    assert summary["products"] == summary["text_available"] == 1
    raw = (output / "fulfillment-catalog.private.json").read_text()
    payload = json.loads(raw)
    record = payload["records"][0]
    assert record["delivery_text"] == compose_delivery_message({
        "title": "Synthetic product", "share_url": "https://pan.baidu.com/s/synthetic", "share_code": "FAKE"})
    assert record["share_verified_at"] is not None
    assert record["share_revision"] is not None
    assert record["stable_product_id"] is None
    assert record["online_status"] == "not_checked"
    assert all(secret not in raw for secret in ("BUYER_MUST_NOT_EXPORT", "CHAT_MUST_NOT_EXPORT", "KNOWLEDGE_MUST_NOT_EXPORT"))
    assert fingerprint(existing_db) == before
    assert output.stat().st_mode & 0o777 == 0o700
    for file in output.iterdir():
        assert file.stat().st_mode & 0o777 == 0o600
    assert "复制不等于已发送" in (output / "fulfillment-catalog.private.md").read_text()


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE products SET share_needs_review=1", "SHARE_NEEDS_REVIEW"),
    ("UPDATE products SET share_verified=0", "SHARE_UNVERIFIED"),
    ("UPDATE products SET share_url=''", "SHARE_MISSING"),
    ("UPDATE products SET share_url='https://example.invalid/expired'", "SHARE_SYNTAX_INVALID"),
    ("UPDATE products SET quality_status='failed'", "QUALITY_BLOCKED"),
    ("DELETE FROM account_products", "ACCOUNT_MAPPING_MISSING"),
    ("UPDATE account_products SET enabled=0", "NO_ELIGIBLE_LISTING_MAPPING"),
    ("UPDATE account_products SET listing_status='paused'", "NO_ELIGIBLE_LISTING_MAPPING"),
    ("UPDATE account_listings SET matched_product_dir_name=NULL", "NO_ELIGIBLE_LISTING_MAPPING"),
])
def test_blocked_records_have_no_sendable_text_or_raw_shares(existing_db, sql, reason):
    with sqlite3.connect(existing_db) as c:
        c.execute(sql)
    before = fingerprint(existing_db)
    catalog = read_catalog(existing_db)
    record = catalog["records"][0]
    assert not record["can_provide_delivery_text"]
    assert record["delivery_text"] is None
    assert reason in record["issues"]
    assert "pan.baidu.com" not in json.dumps(record)
    assert "FAKE" not in json.dumps(record)
    assert fingerprint(existing_db) == before


def test_diagnostics_stdout_has_counts_only(existing_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["catalog", "--database", str(existing_db)])
    assert main() == 0
    text = capsys.readouterr().out
    assert json.loads(text)["products"] == 1
    assert all(value not in text for value in ("synthetic", "FAKE", "pan.baidu", "BUYER"))


def test_operator_known_expired_share_is_blocked_without_db_change(existing_db):
    before = fingerprint(existing_db)
    record = read_catalog(existing_db, blocked_product_refs=("01-synthetic",))["records"][0]
    assert record["delivery_text"] is None
    assert "OPERATOR_REPORTED_UNUSABLE" in record["issues"]
    assert record["online_status"] == "not_checked"
    assert fingerprint(existing_db) == before
    with pytest.raises(CatalogError, match="UNKNOWN_BLOCKED_PRODUCT_REF"):
        read_catalog(existing_db, blocked_product_refs=("unknown-ref",))


def test_missing_database_never_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        export_catalog(path, tmp_path / "export")
    assert not path.exists()
    assert not (tmp_path / "export").exists()


def test_existing_output_refused(existing_db, tmp_path):
    output = tmp_path / "export"
    export_catalog(existing_db, output)
    old = {f.name: f.read_bytes() for f in output.iterdir()}
    with pytest.raises(CatalogError, match="OUTPUT_ALREADY_EXISTS"):
        export_catalog(existing_db, output)
    assert old == {f.name: f.read_bytes() for f in output.iterdir()}


def test_repository_and_symlink_outputs_refused(existing_db, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: synthetic")
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    for output in (repo / "export", alias / "export"):
        with pytest.raises(CatalogError, match="OUTSIDE_GIT"):
            export_catalog(existing_db, output)
    assert not (repo / "export").exists()


def test_private_filenames_are_ignored():
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "check-ignore", "--stdin"], cwd=repo, input=
        "fulfillment-catalog.private.json\nbackup/fulfillment-catalog.private.md\n", text=True, capture_output=True)
    assert result.returncode == 0
    assert len(result.stdout.splitlines()) == 2


def test_sidecar_requires_consistent_backup(existing_db):
    Path(str(existing_db) + "-wal").touch()
    with pytest.raises(CatalogError, match="STANDALONE_SQLITE_BACKUP_REQUIRED"):
        read_catalog(existing_db)


def test_unsupported_schema_not_migrated(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE products(dir_name TEXT)")
    before = fingerprint(path)
    with pytest.raises(CatalogError, match="UNSUPPORTED_SCHEMA"):
        read_catalog(path)
    assert fingerprint(path) == before


def test_prefix_ids_are_distinct(existing_db):
    product = {"enabled_for_account": True, "listing_status": "published", "listing_url": "https://www.goofish.com/item?id=123456789"}
    assert not matches_registered_listing(product, "12345678")
    with sqlite3.connect(existing_db) as c:
        c.row_factory = sqlite3.Row
        row = dict(c.execute("SELECT * FROM products").fetchone())
        row["dir_name"] = "02-collision"
        c.execute("INSERT INTO products (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values()))
        c.execute("INSERT INTO account_products(account_id,product_dir_name,enabled,listing_url,listing_status) VALUES(1,'02-collision',1,'https://www.goofish.com/item?id=123456789','published')")
    record = read_catalog(existing_db)["records"][0]
    assert record["delivery_text"] is not None
    assert not record["mapping_issues"]


def test_standalone_works_without_site_packages_or_runtime(existing_db, tmp_path):
    env = {**os.environ, "XIANYU_MANAGER_DATA_DIR": str(tmp_path / "never-created"),
           "XIANYU_MANAGER_SAFE_MODE": "true"}
    code = """
import json, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('network forbidden')
socket.socket = forbidden
from xianyu_manager.offline_catalog import read_catalog
from pathlib import Path
print(json.dumps(read_catalog(Path(sys.argv[1]))['summary']))
assert all(name not in sys.modules for name in ('fastapi', 'playwright', 'httpx',
    'xianyu_manager.app', 'xianyu_manager.database', 'xianyu_manager.delivery'))
"""
    result = subprocess.run([sys.executable, "-S", "-c", code, str(existing_db)],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["text_available"] == 1
    assert not (tmp_path / "never-created").exists()
