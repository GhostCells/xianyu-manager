"""Strict inventory observation. Never scans assets or changes product settings."""
import json
import re
from datetime import datetime, timezone

from .fulfillment_rules import parse_listing_id


class InventoryObservation:
    def __init__(self):
        self.items = {}
        self.pages = []
        self.complete = False

    def consume(self, data, page):
        if not isinstance(data, dict) or not isinstance(data.get('cardList'), list):
            raise ValueError('INVENTORY_FORMAT_INVALID')
        more = data.get('nextPage')
        if type(more) is not bool:
            raise ValueError('INVENTORY_PAGINATION_UNCONFIRMED')
        cards = data['cardList']
        if more and not cards:
            raise ValueError('INVENTORY_EMPTY_CONTINUATION')
        for card in cards:
            d = card.get('cardData') if isinstance(card, dict) else None
            if not isinstance(d, dict):
                raise ValueError('INVENTORY_CARD_INVALID')
            iid = str(d.get('id') or '').strip()
            title = ' '.join(str(d.get('title') or '').split())
            status = str(d.get('itemStatus'))
            if not re.fullmatch(r'[0-9]{8,30}', iid) or not title or not re.fullmatch(r'-?[0-9]+(?:\.0)?', status):
                raise ValueError('INVENTORY_ITEM_INVALID')
            item = dict(item_id=iid, title=title[:160], item_status=int(float(status)))
            if iid in self.items and self.items[iid] != item:
                raise ValueError('INVENTORY_DUPLICATE_CONFLICT')
            self.items[iid] = item
        self.pages.append(dict(page=page, raw_cards=len(cards), next_page=more))
        self.complete = more is False

    def result(self):
        if not self.complete:
            raise ValueError('INVENTORY_INCOMPLETE_KEEP_PREVIOUS')
        items = list(self.items.values())
        return dict(items=items, pages=self.pages, pagination_complete=True,
                    raw_count=sum(p['raw_cards'] for p in self.pages),
                    unique_count=len(items), normal_count=sum(i['item_status'] == 0 for i in items),
                    special_count=sum(i['item_status'] != 0 for i in items),
                    observed_at=datetime.now(timezone.utc).isoformat())


def status(connection, account_id):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_refresh_state'").fetchone()
    row = connection.execute('SELECT report FROM inventory_refresh_state WHERE account_id=?', (account_id,)).fetchone() if exists else None
    return json.loads(row[0]) if row else {'account_id': account_id, 'last_success_at': None, 'needs_refresh': True}


def store(connection, account_id, report):
    """Caller owns transaction. Incomplete results cannot deactivate old rows."""
    if report.get('pagination_complete') is not True:
        raise ValueError('INVENTORY_INCOMPLETE_KEEP_PREVIOUS')
    items = report['items']
    connection.execute('CREATE TABLE IF NOT EXISTS inventory_refresh_state(account_id INTEGER PRIMARY KEY,report TEXT NOT NULL)')
    bindings = connection.execute('SELECT product_dir_name,listing_url FROM account_products WHERE account_id=?', (account_id,)).fetchall()
    connection.execute('UPDATE account_listings SET is_active=0 WHERE account_id=?', (account_id,))
    for item in items:
        iid = item['item_id']
        names = {r[0] for r in bindings if parse_listing_id(r[1] or '') == iid}
        # New rows may mirror an already explicit ID binding; never fuzzy-match.
        matched = next(iter(names)) if len(names) == 1 else None
        connection.execute('''INSERT INTO account_listings
            (account_id,item_id,title,listing_url,source_text,source_kind,matched_product_dir_name,is_active,synced_at)
            VALUES(?,?,?,?,?,'platform_inventory',?,?,?)
            ON CONFLICT(account_id,item_id) DO UPDATE SET
            title=excluded.title,source_text=excluded.source_text,source_kind=excluded.source_kind,
            is_active=excluded.is_active,synced_at=excluded.synced_at''',
            (account_id,iid,item['title'],'https://www.goofish.com/item?id='+iid,
             json.dumps({'itemStatus':item['item_status']}),matched,int(item['item_status']==0),report['observed_at']))
    saved = {**report, 'account_id':account_id, 'last_success_at':report['observed_at'], 'needs_refresh':False}
    connection.execute('INSERT INTO inventory_refresh_state VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET report=excluded.report',
                       (account_id,json.dumps(saved,ensure_ascii=False)))
    return saved


def mark_failed(connection, account_id, code):
    previous = status(connection, account_id)
    previous.update(needs_refresh=True, last_error=code,
                    last_attempt_at=datetime.now(timezone.utc).isoformat())
    connection.execute('CREATE TABLE IF NOT EXISTS inventory_refresh_state(account_id INTEGER PRIMARY KEY,report TEXT NOT NULL)')
    connection.execute('INSERT INTO inventory_refresh_state VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET report=excluded.report',
                       (account_id,json.dumps(previous,ensure_ascii=False)))
