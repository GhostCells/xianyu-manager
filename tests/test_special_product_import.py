import pytest
from test_product_import import intake, uploaded

IDS = ['1075865170356', '1072681555598', '1074719040968']


@pytest.mark.parametrize('iid', IDS)
def test_approved_special_import_end_to_end(intake, monkeypatch, iid):
    imp, account = intake
    assert account == 2
    monkeypatch.setenv('XIANYU_MANAGER_SPECIAL_STATUS_ITEMS', ','.join(IDS))
    with imp.db.connect() as c:
        c.execute('UPDATE account_listings SET item_id=?,listing_url=?,source_text=?',
                  (iid, 'https://www.goofish.com/item?id='+iid, '{"itemStatus":-9}'))
        before = {t: [tuple(r) for r in c.execute('SELECT * FROM '+t)]
                  for t in ('accounts', 'orders', 'automation_outbound_events')}
    token = uploaded(intake, item_id=iid)
    preview = imp.preview(account, token, None)
    assert imp.confirm(account, token, preview['preview_id'], True)['committed']
    with imp.db.connect() as c:
        assert c.execute('SELECT source_text FROM account_listings').fetchone()[0] == '{"itemStatus":-9}'
        for t, rows in before.items():
            assert [tuple(r) for r in c.execute('SELECT * FROM '+t)] == rows


@pytest.mark.parametrize('status,source,account,allowed', [
    ('{"itemStatus":-9}', 'platform_inventory', 2, False),
    ('{"itemStatus":-9}', 'platform_inventory', 1, True),
    ('{"itemStatus":1}', 'platform_inventory', 2, True),
    ('{"itemStatus":-9}', 'manual', 2, True),
    ('broken', 'platform_inventory', 2, True),
])
def test_unapproved_status_stays_blocked(intake, monkeypatch, status, source, account, allowed):
    imp, _ = intake
    monkeypatch.setenv('XIANYU_MANAGER_SPECIAL_STATUS_ITEMS', '123456789' if allowed else '')
    with imp.db.connect() as c:
        c.execute('UPDATE account_listings SET source_text=?,source_kind=?,account_id=?', (status, source, account))
    with pytest.raises(ValueError, match='请先刷新在售列表'):
        uploaded((imp, account))


def test_downlisted_after_preview_cannot_confirm(intake, monkeypatch):
    imp, account = intake
    monkeypatch.setenv('XIANYU_MANAGER_SPECIAL_STATUS_ITEMS', '123456789')
    with imp.db.connect() as c:
        c.execute('UPDATE account_listings SET source_text=?', ('{"itemStatus":-9}',))
    token = uploaded(intake)
    preview = imp.preview(account, token, None)
    with imp.db.connect() as c:
        c.execute('UPDATE account_listings SET is_active=0')
    with pytest.raises(ValueError):
        imp.confirm(account, token, preview['preview_id'], True)
    assert imp.db.get_product('45-demo') is None
