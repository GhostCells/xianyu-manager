"""Bounded, in-memory follow-up of live group cards; never an order scanner."""
import asyncio
import re
import time

from .order_cutoff import require_after_cutoff
from .fulfillment_rules import delivery_issues


class LiveGroupWatch:
    INTERVAL = 30
    MAX_PROBES = 20
    MAX_SECONDS = 600
    MAX_ACTIVE = 5
    MAX_SEEN = 256

    def __init__(self, service, library):
        self.service = service
        self.library = library
        self.tasks = {}
        self.seen = set()
        self.query_lock = asyncio.Lock()

    def audit(self, order_id, reason):
        self.service.database.record_audit('live_group_watch', order_id,
            {'account_id': self.service.runtime_policy.account_id, 'reason': reason})

    def valid_runtime(self, account_id, websocket):
        s = self.service
        p = s.runtime_policy
        account = s.database.get_account(account_id) or {}
        return (account_id == p.account_id == 2 and p.mvp_fulfillment
                and p.fulfillment_enabled and not p.order_recovery_enabled
                and account.get('delivery_enabled') and account.get('binding_status') == 'bound'
                and s._account_id == account_id and s._status == 'listening'
                and s._runtime_websocket is websocket)

    def schedule(self, account_id, websocket, order_id, item_id, buyer_id, event_ms):
        if not self.valid_runtime(account_id, websocket) or self.library is None:
            return
        if not re.fullmatch(r'[0-9]{10,30}', order_id or ''):
            self.audit('', 'LIVE_ORDER_ID_MISSING'); return
        now_ms = int(time.time()*1000)
        started = getattr(self.service, '_reply_listen_started_ms', now_ms)
        if event_ms is None or not started < event_ms <= now_ms+5000:
            self.audit(order_id, 'NON_LIVE_EVENT_REJECTED'); return
        if order_id in self.seen:
            return
        if len(self.tasks) >= self.MAX_ACTIVE or len(self.seen) >= self.MAX_SEEN:
            self.audit(order_id, 'LIVE_WATCH_CAPACITY_REACHED'); return
        self.seen.add(order_id)
        task = asyncio.create_task(self.run(account_id, websocket, order_id, item_id, buyer_id))
        self.tasks[order_id] = task
        def done(finished):
            if self.tasks.get(order_id) is finished:
                self.tasks.pop(order_id, None)
        task.add_done_callback(done)

    async def run(self, account_id, websocket, order_id, event_item, event_buyer):
        s = self.service
        deadline = time.monotonic()+self.MAX_SECONDS
        identity = None
        try:
            for index in range(self.MAX_PROBES):
                if index:
                    await asyncio.sleep(self.INTERVAL)
                if time.monotonic() >= deadline or not self.valid_runtime(account_id, websocket):
                    self.audit(order_id, 'WINDOW_OR_SESSION_ENDED'); return
                s.runtime_policy.require_fulfillment()
                with s.database.connect() as c:
                    if (c.execute('SELECT 1 FROM orders WHERE xianyu_order_id=?',(order_id,)).fetchone()
                            or c.execute("SELECT 1 FROM automation_outbound_events WHERE kind='delivery' AND reference=?",('order:'+order_id,)).fetchone()):
                        self.audit(order_id, 'ORDER_ALREADY_HANDLED'); return
                async with self.query_lock:
                    if time.monotonic() >= deadline or not self.valid_runtime(account_id, websocket):
                        return
                    preview = await asyncio.wait_for(s._fetch_recent_sold_orders(
                        s._runtime_cookie_map, exact_order_id=order_id), timeout=25)
                rows = preview.get('orders', [])
                if len(rows) != 1 or rows[0].get('order_id') != order_id:
                    raise ValueError('EXACT_ORDER_REQUIRED')
                row = rows[0]
                item, buyer = row.get('item_id'), row.get('buyer_id')
                if (not re.fullmatch(r'[0-9]{8,}',str(item or ''))
                        or not re.fullmatch(r'[0-9]{5,}',str(buyer or ''))
                        or (event_item and item != event_item) or (event_buyer and buyer != event_buyer)):
                    raise ValueError('ORDER_IDENTITY_MISMATCH')
                payment = require_after_cutoff(s.runtime_policy.order_cutoff_at, row.get('paid_time'))
                current = (item,buyer,payment.isoformat())
                if identity is not None and current != identity:
                    raise ValueError('ORDER_IDENTITY_CHANGED')
                identity = current
                s.runtime_policy.require_delivery_item(item)
                product = s.database.get_product_by_listing_item_id(item, account_id)
                if product is None or delivery_issues(product):
                    raise ValueError('PRODUCT_NOT_QUALIFIED')
                if not self.valid_runtime(account_id, websocket) or time.monotonic() >= deadline:
                    return
                if row.get('order_status') in {'待发货','pending_ship'}:
                    # Re-read exact platform state and reuse the complete existing
                    # package/manual/claim/idempotency/send/confirm chain.
                    await s.approved_order(account_id, order_id, library=self.library, execute=True)
                    self.audit(order_id, 'NORMAL_CHAIN_FINISHED'); return
                if row.get('order_status') in {'交易关闭','已关闭','已退款','退款成功','待付款','交易成功','待收货'}:
                    self.audit(order_id, 'ORDER_NO_LONGER_WAITING'); return
                if index == 0:
                    self.audit(order_id, 'WAITING_FOR_MANUAL_GROUP_COMPLETION')
            self.audit(order_id, 'LIVE_WATCH_EXHAUSTED')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # No raw platform response, recipient or credentials in diagnostics.
            code = str(exc)
            if not code.isascii() or not all(ch.isupper() or ch=='_' for ch in code):
                code = type(exc).__name__
            self.audit(order_id, 'STOPPED_'+code[:100])

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.seen.clear()
