"""One explicitly permitted inventory read; no business listeners or DB writes."""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

from .fulfillment_rules import delivery_issues, parse_listing_id
from .runtime_policy import RuntimeOperationBlocked


def diagnose_cards(cards, page, seen):
    """Observe existing projection rules without changing their output."""
    from .delivery import inventory_cards_to_raw_items
    counts = dict.fromkeys(('card_data_parseable', 'status_passed', 'status_excluded',
                           'invalid_id', 'empty_title', 'invalid_card_data',
                           'invalid_card', 'same_page_duplicates', 'cross_page_duplicates'), 0)
    excluded, local = [], set()
    for card in cards:
        data = card.get('cardData', {}) if isinstance(card, dict) else None
        reason = None
        if not isinstance(card, dict):
            reason = 'invalid_card'
        elif not isinstance(data, dict):
            reason = 'invalid_card_data'
        else:
            counts['card_data_parseable'] += 1
            if str(data.get('itemStatus')) not in {'0', '0.0'}:
                reason = 'status_excluded'
            else:
                counts['status_passed'] += 1
                item_id = str(data.get('id') or '').strip()
                if not item_id.isdigit() or len(item_id) < 8:
                    reason = 'invalid_id'
                elif not ' '.join(str(data.get('title') or '').split()):
                    reason = 'empty_title'
        if reason:
            counts[reason] += 1
            if isinstance(data, dict):
                item_id = str(data.get('id') or '').strip()
                if item_id.isdigit() and len(item_id) >= 8:
                    status = data.get('itemStatus')
                    excluded.append({'item_id': item_id,
                        'title': ' '.join(str(data.get('title') or '').split())[:160],
                        'itemStatus': status if isinstance(status, (int, float, bool)) or status is None
                            else status if isinstance(status, str) and re.fullmatch(r'-?\d+(\.\d+)?', status) else '[unrecognized]',
                        'exclusion_reason': reason, 'page_number': page})
    valid = inventory_cards_to_raw_items(cards)
    for raw in valid:
        item_id = parse_listing_id(raw['url'])
        if item_id in local:
            counts['same_page_duplicates'] += 1
        elif item_id in seen:
            counts['cross_page_duplicates'] += 1
        local.add(item_id)
    return {'page_number': page, 'raw_cards': len(cards), **counts,
            'valid_before_dedup': len(valid), 'unique_cumulative': len(seen | local),
            'excluded': excluded}


def match_items(items, products, listings):
    products_by_name = {p['dir_name']: p for p in products}
    result = []
    for item in items:
        item_id = item['item_id']
        names = {p['dir_name'] for p in products
                 if parse_listing_id(str(p.get('listing_url') or '')) == item_id}
        names.update(str(p['matched_product_dir_name']) for p in listings
                     if str(p['item_id']) == item_id and p.get('matched_product_dir_name'))
        product = products_by_name.get(next(iter(names))) if len(names) == 1 else None
        issues = ['MAPPING_CONFLICT'] if len(names) > 1 else ['MAPPING_MISSING'] if not product else []
        if product:
            issues.extend(delivery_issues(product))
            if product.get('catalog_status') == 'listing_only':
                issues.append('LISTING_ONLY')
        result.append({
            'item_id': item_id, 'title': item['title'],
            'local_product': product['dir_name'] if product else None,
            'package_clear': bool(product) and 'DELIVERY_PACKAGE_UNCONFIRMED' not in issues,
            'share_registered': bool(product and product.get('share_url')),
            'fingerprint_missing': bool(product and not product.get('verified_fingerprint')),
            'issues': issues, 'business_allowed': False,
        })
    return result


def report_directory(database):
    return database.path.parent / 'preparation-inventory'


def latest_report(database, account_id):
    paths = sorted(report_directory(database).glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in paths:
        data = json.loads(path.read_text())
        if data.get('account_id') == account_id:
            return data
    return {'account_id': account_id, 'attempted': False, 'items': [], 'complete': False}


async def collect_once(owner, account_id, frontend_count=None):
    # Deliberately reuse only pure protocol helpers, not DeliveryService methods.
    from .delivery import MTOP_APP_KEY, ITEM_LIST_URL, generate_mtop_sign, inventory_cards_to_raw_items

    policy = owner.runtime_policy
    policy.require_account(account_id)
    approval = policy.preparation_permission('inventory_once')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', approval):
        raise RuntimeOperationBlocked('INVALID_PREPARATION_APPROVAL_ID')
    directory = report_directory(owner.database)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (approval + '.json')
    report = {'account_id': account_id, 'attempted': True, 'complete': False,
              'started_at': datetime.now(timezone.utc).isoformat(), 'pages': 0,
              'items': [], 'business_allowed': False, 'status': 'in_progress',
              'diagnostic_snapshot': True, 'page_diagnostics': [],
              'pagination_complete': False, 'complete_basis': None,
              'frontend_expected_count': frontend_count, 'frontend_count_reconciled': False}
    try:
        with path.open('x') as f:
            json.dump(report, f)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        raise RuntimeOperationBlocked('INVENTORY_ATTEMPT_ALREADY_CONSUMED') from None
    items = {}
    try:
        cookies = {c['name']: c['value'] for c in await owner._context.cookies('https://www.goofish.com/')}
        token = cookies.get('_m_h5_tk', '').split('_', 1)[0]
        seller = cookies.get('unb', '')
        if not token or not seller:
            raise RuntimeOperationBlocked('LOGIN_SESSION_INCOMPLETE')
        continuation = {}
        for page in range(1, 6):
            if page > 1:
                await asyncio.sleep(2)
            if policy.preparation_permission('inventory_once') != approval:
                raise RuntimeOperationBlocked('PREPARATION_APPROVAL_CHANGED')
            body = json.dumps({'needGroupInfo': page == 1, 'pageNumber': page,
                               'pageSize': 20, 'userId': seller, **continuation},
                              ensure_ascii=False, separators=(',', ':'))
            timestamp = str(int(time.time() * 1000))
            response = await owner._context.request.post(
                ITEM_LIST_URL, params={'jsv': '2.7.2', 'appKey': MTOP_APP_KEY,
                    't': timestamp, 'sign': generate_mtop_sign(timestamp, token, body),
                    'v': '1.0', 'type': 'originaljson', 'accountSite': 'xianyu',
                    'dataType': 'json', 'timeout': '20000',
                    'api': 'mtop.idle.web.xyh.item.list', 'sessionOption': 'AutoLoginOnly'},
                form={'data': body}, headers={'origin': 'https://www.goofish.com',
                    'referer': 'https://www.goofish.com/'}, timeout=20000,
                max_redirects=0)
            try:
                if response.status != 200:
                    raise RuntimeOperationBlocked('INVENTORY_HTTP_STOPPED')
                payload = await response.json()
            finally:
                await response.dispose()
            if not isinstance(payload, dict) or not isinstance(payload.get('ret'), list) or not any(
                    str(r).startswith('SUCCESS::') for r in payload['ret']):
                raise RuntimeOperationBlocked('PLATFORM_VERIFICATION_OR_RESPONSE_STOPPED')
            data = payload.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('cardList'), list):
                raise RuntimeOperationBlocked('INVENTORY_FORMAT_UNCONFIRMED')
            diagnostic = diagnose_cards(data['cardList'], page, set(items))
            # Persist only scalar pagination metadata; opaque cursor objects can
            # contain account data and must not be copied into diagnostics.
            for key in ('nextPage', 'nextPageModel', 'nextPageNum'):
                value = data.get(key)
                diagnostic[key] = value if value is None or isinstance(value, (bool, int, float)) else {
                    'redacted': True, 'type': type(value).__name__}
                diagnostic[key + '_present'] = key in data
            diagnostic['pagination_ended'] = data.get('nextPage') is False
            report['page_diagnostics'].append(diagnostic)
            for raw in inventory_cards_to_raw_items(data['cardList']):
                item_id = parse_listing_id(raw['url'])
                items[item_id] = {'item_id': item_id, 'title': raw['title']}
            report['pages'] = page
            if data.get('nextPage') is False:
                report['complete'] = True
                report['pagination_complete'] = True
                report['complete_basis'] = 'API_NEXT_PAGE_FALSE'
                break
            if data.get('nextPage') is not True or not data['cardList']:
                break
            continuation = {k: data[k] for k in ('nextPageModel', 'nextPageNum') if k in data}
        report['status'] = 'complete' if report['complete'] else 'incomplete_no_offsale_inference'
    except Exception:
        # Never persist upstream errors/URLs/cookies or automatically retry verification.
        report['status'] = 'stopped_requires_operator_review'
    finally:
        pages = report['page_diagnostics']
        report['totals'] = {key: sum(p[key] for p in pages) for key in (
            'raw_cards', 'valid_before_dedup', 'status_excluded', 'invalid_id',
            'empty_title', 'invalid_card_data', 'invalid_card',
            'same_page_duplicates', 'cross_page_duplicates')}
        report['totals']['unique_ids'] = len(items)
        report['frontend_count_reconciled'] = bool(report['pagination_complete']
            and frontend_count is not None and len(items) == frontend_count)
        with owner.database.connect() as connection:
            listings = [dict(row) for row in connection.execute(
                'SELECT item_id,matched_product_dir_name FROM account_listings WHERE account_id=?',
                (account_id,))]
        report['items'] = match_items(list(items.values()),
            owner.database.list_products(account_id, include_listing_only=True), listings)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        with path.open('w') as f:
            json.dump(report, f, ensure_ascii=False)
    return report
