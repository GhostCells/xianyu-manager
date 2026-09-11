"""Read-only prerequisites for explicitly approved single-order evaluation."""
import hashlib
import re
from pathlib import Path


def require_fresh_order(database, account_id, order_id, buyer_id, item_id, *,
                        allow_missing_chat=False, resolved_chat_id=None):
    with database.connect() as connection:
        # Refuse even a prior failed/uncertain attempt; never turn this into recovery.
        if connection.execute('SELECT 1 FROM orders WHERE xianyu_order_id=?', (order_id,)).fetchone():
            raise ValueError('APPROVED_ORDER_ALREADY_RECORDED')
        if connection.execute(
            "SELECT 1 FROM automation_outbound_events WHERE kind='delivery' AND reference=?",
            ('order:' + order_id,),
        ).fetchone():
            raise ValueError('APPROVED_ORDER_OUTBOUND_EXISTS')
        rows = connection.execute(
            'SELECT chat_id FROM chat_sessions WHERE account_id=? AND buyer_id=? AND listing_item_id=?',
            (account_id, buyer_id, item_id),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError('APPROVED_ORDER_CHAT_NOT_UNIQUE')
        if not rows and resolved_chat_id is None:
            if not allow_missing_chat:
                raise ValueError('APPROVED_ORDER_CHAT_NOT_UNIQUE')
            if not database._account_can_deliver(connection, account_id, ''):
                raise ValueError('APPROVED_ORDER_ACCOUNT_OR_MANUAL_BLOCK')
            return None
        chat_id = rows[0]['chat_id'] if rows else resolved_chat_id
        if resolved_chat_id is not None and chat_id != resolved_chat_id:
            raise ValueError('APPROVED_ORDER_CHAT_CONFLICT')
        if not re.fullmatch(r'[0-9]{5,}', str(chat_id)):
            raise ValueError('APPROVED_ORDER_CHAT_INVALID')
        existing = connection.execute(
            'SELECT buyer_id,listing_item_id FROM chat_sessions WHERE account_id=? AND chat_id=?',
            (account_id,chat_id),
        ).fetchone()
        if existing and (existing['buyer_id'] != buyer_id or existing['listing_item_id'] != item_id):
            raise ValueError('APPROVED_ORDER_CHAT_CONFLICT')
        if not database._account_can_deliver(connection, account_id, chat_id):
            raise ValueError('APPROVED_ORDER_ACCOUNT_OR_MANUAL_BLOCK')
        return chat_id


def require_current_package(library, product):
    if product.get('delivery_kind','zip') == 'cloud':
        from .delivery_package import check_registered_cloud
        check_registered_cloud(library, product)
        return
    root = Path(library).resolve(strict=True)
    directory = (root / product['dir_name']).resolve(strict=True)
    if directory == root or not directory.is_relative_to(root):
        raise ValueError('APPROVED_ORDER_PACKAGE_PATH_INVALID')
    name = str(product.get('zip_name') or '')
    if Path(name).name != name or not name:
        raise ValueError('APPROVED_ORDER_PACKAGE_PATH_INVALID')
    packages = [p for p in directory.rglob('*') if p.is_file() and p.suffix.lower() == '.zip']
    if len(packages) != 1 or packages[0].name != name:
        raise ValueError('APPROVED_ORDER_PACKAGE_AMBIGUOUS')
    package = packages[0].resolve(strict=True)
    if not package.is_relative_to(directory) or package.stat().st_size != product['zip_size']:
        raise ValueError('APPROVED_ORDER_PACKAGE_CHANGED')
    digest = hashlib.sha256()
    with package.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != product['zip_hash']:
        raise ValueError('APPROVED_ORDER_PACKAGE_CHANGED')
