# Independent production inventory refresh

`POST /api/listings/refresh` reuses the listening runtime's account/session and existing protected MTOP transport. It never launches a browser, scans the product library, calls reconciliation, or sends business messages. `GET /api/listings/refresh-status` exposes the last successful observation and failure state.

- At most five pages, 20 requested cards per page, two seconds between pages; one shared refresh lock and 60-second attempt cooldown.
- Every card must have a reliable ID, title and numeric status. Pagination must explicitly terminate with boolean `nextPage=false`. Unknown/malformed/partial responses preserve the old inventory.
- Successful complete empty results are supported. This is API pagination completeness, not proof of agreement with the platform UI count.
- Only normal status 0 rows are active; special statuses are retained inactive and listed in the refresh summary. Missing historical rows remain stored inactive, not declared platform-deleted.
- Existing mappings, Product fields, knowledge, shares, verification, accounts, orders and runtime policy are not modified. Newly observed rows can mirror an already explicit account/product ID binding; no title matching or automatic Product creation.
- A transaction updates account_listings and a small inventory_refresh_state table. Failed reads only record refresh failure metadata, preserving the last successful timestamp and listing set.

## Deployment boundary

Backend loading requires a controlled manager restart with the present architecture; that restart also affects its owned Chrome. Do not claim this button is live merely because source/static files have been copied. Obtain the restart window, preserve Profile and policy/cutoff, back up the database and changed source, then deploy together. No cold reboot or network changes are needed.

For rollback, restore only changed code after stopping the service in the approved window. The extra table is additive and can remain. Do not restore the entire database over newer production orders. If reverting inventory changes is needed, restore only the affected account_listings fields from the pre-refresh backup after checking for intervening operator edits.

## Verification

Synthetic tests cover strict pagination, empty results, special status, dedup/conflicts, malformed cards, failure preservation, unchanged Product/account/order data, session change, cooldown and safe/prepare rejection. Browser UI validation must use synthetic endpoints, not trigger production platform refresh inadvertently.
