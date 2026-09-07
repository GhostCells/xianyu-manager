"""UTC payment boundary. Never infer a payment time from receipt/creation time."""
from datetime import datetime, timezone
import re


class OrderCutoffBlocked(ValueError):
    pass


def utc_time(value, *, field):
    if isinstance(value, bool) or value is None:
        raise OrderCutoffBlocked(f'{field}_MISSING_OR_INVALID')
    text = str(value).strip()
    try:
        # The seller API may use Unix seconds or milliseconds; no other units.
        if re.fullmatch(r'\d{10}|\d{13}', text):
            result = datetime.fromtimestamp(int(text) / (1000 if len(text) == 13 else 1), timezone.utc)
        else:
            result = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if result.tzinfo is None or result.utcoffset() is None:
                raise ValueError('timezone required')
            result = result.astimezone(timezone.utc)
        return result
    except (ValueError, OverflowError, OSError):
        raise OrderCutoffBlocked(f'{field}_MISSING_OR_INVALID') from None


def require_after_cutoff(cutoff, paid_time):
    boundary = utc_time(cutoff, field='ORDER_CUTOFF')
    payment = utc_time(paid_time, field='PLATFORM_PAYMENT_TIME')
    if payment <= boundary:
        raise OrderCutoffBlocked('ORDER_AT_OR_BEFORE_CUTOFF')
    return payment
