"""Classify cross-account asset references without changing any association."""


def has_foreign_product_use(connection, name, account_id):
    # Archived migration drafts with no listing/order are catalog placeholders,
    # not a live account's ownership. Keep their records, but don't block intake.
    if connection.execute(
        'SELECT 1 FROM account_listings WHERE matched_product_dir_name=? AND account_id<>?',
        (name, account_id),
    ).fetchone():
        return True
    rows = connection.execute('''SELECT ap.account_id, ap.listing_status, ap.listing_url,
        a.is_archived, a.is_active, a.delivery_enabled, a.binding_status
        FROM account_products ap LEFT JOIN accounts a ON a.id=ap.account_id
        WHERE ap.product_dir_name=? AND ap.account_id<>?''', (name, account_id)).fetchall()
    for other, status, url, archived, active, delivery, binding in rows:
        if not (archived == 1 and active == 0 and delivery == 0
                and binding in ('unbound', 'expired') and status == 'draft' and not str(url or '').strip()):
            return True
        if connection.execute('SELECT 1 FROM orders WHERE account_id=? AND product_dir_name=?', (other, name)).fetchone():
            return True
    return False
