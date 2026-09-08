"""Small in-process gate; no IO, no authority to enable any business setting."""
class RecoveryGate:
    def __init__(self):
        self.blocked = False
        self.since = None
        self.samples = set()
        self.last_attempt = float('-inf')

    def block(self):
        self.blocked = True
        self.since = None
        self.samples.clear()

    def ready(self, *, checked_at, now):
        if not self.blocked:
            return False
        if self.since is None:
            self.since = now
        self.samples.add(checked_at)
        if len(self.samples) < 2 or now-self.since < 10 or now-self.last_attempt < 60:
            return False
        self.last_attempt = now
        self.blocked = False
        return True


def recovery_allowed(policy, *, delivery_enabled=False):
    return (policy.resident_reply and policy.mode == 'normal'
            and policy.account_id == 2 and not policy.order_recovery_enabled
            and (policy.reply_only or (policy.mvp_fulfillment and not delivery_enabled)))
