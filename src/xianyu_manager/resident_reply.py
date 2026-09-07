"""Restore the same owner after fresh egress readiness, never recover orders."""
import asyncio


async def restore_reply_owner(policy, session, delivery, database, *, wait=asyncio.sleep):
    mvp = getattr(policy, 'mvp_fulfillment', False)
    if not policy.resident_reply or policy.mode != 'normal' or not (policy.reply_only or mvp):
        return 'disabled'
    if (policy.fulfillment_enabled and not mvp) or policy.order_recovery_enabled:
        raise RuntimeError('RESIDENT_REPLY_POLICY_INVALID')
    # Do not latch a startup race before the root producer has inspected this PID.
    for _ in range(60):
        if policy.egress_status()['ready']:
            break
        await wait(1)
    else:
        return 'egress_not_ready'
    result = await session.start_login(policy.account_id)
    if not result.get('login_detected'):
        return 'manual_verification_required'
    await session.confirm_login(policy.account_id)
    if database.get_auto_reply_settings(policy.account_id).get('enabled'):
        await delivery.start_auto_reply(policy.account_id)
        return 'reply_start_requested'
    return 'owner_ready_reply_disabled'
