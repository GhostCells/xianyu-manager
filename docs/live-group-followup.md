# Live manual-group completion follow-up

Scope: account 2, existing MVP fulfillment enabled, recovery disabled. A recognized
waiting **system card** received after the current WebSocket began listening may
start an in-memory follow-up for its explicit order ID. Missing/stale timestamps or
missing order IDs are rejected; no broad list, database scan or startup replay occurs.

- Query only that exact platform order, immediately and then every 30 seconds.
- Maximum 20 probes and 600 seconds; at most 5 concurrent watches, with serialized
  lookups and a 25-second lookup timeout. Unknown states never authorize sending.
- Platform payment time must reliably exceed the unchanged cutoff; event time is
  solely a freshness gate, never a payment-time substitute.
- Bind account, order, buyer, item and payment time; reject changed identity.
- When platform explicitly reports pending shipment, reuse `approved_order` and its
  normal payment/claim/send/confirmation chain. Existing package or cloud-version,
  manual takeover, outbound safety and idempotency checks remain in force.
- Do not click automatic group exemption. The user still completes exemption on the
  phone; no second status push is necessary while the watch remains active.
- Any prior order/outbound record, cancellation, query error, expired watch, disabled
  delivery or connection change stops the watch. It never retries a send result.
- Duplicate order cards share one watch. At most 256 distinct watched order IDs per
  connection are retained for deduplication; capacity exhaustion is audited and
  requires operator handling, not silently widened polling. Disconnect/service stop
  cancels and clears all watches. Nothing persists for automatic recovery.

This does **not** cover orders missed while offline, cards without reliable timestamps
or order IDs, or exemption after the bounded window. Such orders require explicit
single-order handling. Follow-up audits use fixed reason codes and no message bodies
or credentials. No existing order is sent merely by deploying this feature.
