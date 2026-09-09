"""Explicit account-scoped compatibility, never rewrite platform status."""
import os
import re


def sellable_status(account_id, item_id, status):
    if str(status) in {'0', '0.0'}:
        return True
    if account_id != 2 or str(status) not in {'-9', '-9.0'}:
        return False
    ids = [x.strip() for x in os.environ.get('XIANYU_MANAGER_SPECIAL_STATUS_ITEMS', '').split(',') if x.strip()]
    if any(not re.fullmatch(r'[0-9]{8,30}', x) for x in ids) or len(ids) != len(set(ids)):
        return False
    return str(item_id) in ids
