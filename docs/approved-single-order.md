# Explicit single-order reevaluation

This is not order recovery. The existing MVP historical reconcile prohibition is unchanged.

- POST `/api/delivery/approved-order/preview`: read-only qualification; no claim, chat creation, or send.
- POST `/api/delivery/approved-order/execute`: only after separate explicit operator approval.
- Both require `X-Order-Action: approved-single-order` and exactly `account_id`, `order_id` in JSON.
- Both require runtime account match, fulfillment permission, actual delivery switch on, recovery off, and a live message session.
- The platform request uses `orderIds` for that exact order, one request/page, and rejects any response other than exactly that order. No unfiltered fallback.
- Preview checks paid time/cutoff, allowlist, mapping, current verification and disk ZIP, unique existing buyer/item chat, manual block, any prior order/outbound record, and safety limits.
- Execute repeats preview and delegates to the existing paid-event handler, which rechecks the target platform order, claim, cutoff, and final outbound guards. Concurrent calls are serialized; prior records are refused, including failed or uncertain attempts.
- Response contains no buyer identity, chat content, credentials or share contents. `executed=true` means the handler ran, not that delivery succeeded; inspect status and sent evidence.
- Nothing schedules this action on startup. Enabling delivery does not replay a consumed payment event.

Deployment: keep actual delivery off through reload, restore the existing account session, then enable only the approved item scope. Run preview only and wait for the user's separate execute approval.
