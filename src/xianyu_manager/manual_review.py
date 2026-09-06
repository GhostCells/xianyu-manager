"""Local operator evidence, never a network send or platform verification."""

import hashlib
import json

from .runtime_policy import RuntimeOperationBlocked


def fingerprint(row):
    return hashlib.sha256(
        json.dumps(dict(row), sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def list_review_orders(database, policy):
    policy.require_account(policy.account_id)
    if policy.safe_mode or policy.mode != "prepare":
        raise RuntimeOperationBlocked("PREPARATION_REVIEW_ONLY")
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM orders WHERE account_id=? ORDER BY id DESC",
            (policy.account_id,),
        ).fetchall()
    fields = (
        "xianyu_order_id",
        "delivery_status",
        "message_sent_at",
        "platform_confirm_status",
        "manual_delivery_state",
        "manual_platform_state",
        "manual_review_revision",
    )
    return [{**{k: r[k] for k in fields}, "fingerprint": fingerprint(r)} for r in rows]


def record_review(
    database,
    policy,
    *,
    account_id,
    order_id,
    expected_fingerprint,
    action,
    platform_state,
    operator,
    reason,
    evidence_ref
):
    if policy.safe_mode or policy.mode != "prepare" or database.safe_mode:
        raise RuntimeOperationBlocked("PREPARATION_REVIEW_ONLY")
    policy.require_account(account_id)
    if action not in {
        "copied",
        "confirmed_sent",
        "confirmed_not_sent",
        "unknown",
        "platform_only",
    }:
        raise ValueError("INVALID_REVIEW_ACTION")
    if platform_state not in {
        "not_checked",
        "reported_confirmed",
        "reported_unconfirmed",
        "unknown",
    }:
        raise ValueError("INVALID_PLATFORM_REPORT")
    if any(not str(v).strip() for v in (operator, reason, evidence_ref)):
        raise ValueError("REVIEW_EVIDENCE_REQUIRED")
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM orders WHERE xianyu_order_id=? AND account_id=?",
            (order_id, account_id),
        ).fetchone()
        if row is None:
            raise ValueError("ORDER_NOT_IN_RUNTIME_ACCOUNT")
        if fingerprint(row) != expected_fingerprint:
            raise ValueError("STALE_ORDER_REVIEW")
        previously_sent = connection.execute(
            "SELECT 1 FROM order_manual_reviews WHERE order_id=? AND action='confirmed_sent'",
            (order_id,),
        ).fetchone()
        if action == "confirmed_not_sent" and (
            row["message_sent_at"] or previously_sent
        ):
            raise ValueError("EXISTING_SEND_EVIDENCE_CONFLICT")
        delivery_state = (
            row["manual_delivery_state"]
            if action in {"copied", "platform_only"}
            else action
        )
        # Original delivery stage, ACK, hashes and attempts remain immutable here.
        connection.execute(
            """INSERT INTO order_manual_reviews
            (account_id,order_id,action,platform_state,operator,reason,evidence_ref,before_fingerprint)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                account_id,
                order_id,
                action,
                platform_state,
                operator,
                reason,
                evidence_ref,
                expected_fingerprint,
            ),
        )
        connection.execute(
            """UPDATE orders SET manual_delivery_state=?,manual_platform_state=?,
            manual_review_revision=manual_review_revision+1 WHERE xianyu_order_id=? AND account_id=?""",
            (delivery_state, platform_state, order_id, account_id),
        )
    return {
        "ok": True,
        "external_action": False,
        "platform_verified_online": False,
        "automatic_retry_authorized": False,
        "delivery_material_unlock": delivery_state
        in {"confirmed_sent", "confirmed_not_sent"},
    }
