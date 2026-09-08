"""Bounded, allowlisted authentication evidence; never raw response text."""
import hashlib
from datetime import datetime, timezone

MESSAGES = {
    'SUCCESS': 'message token accepted',
    'FAIL_SYS_SESSION_EXPIRED': 'platform session expired',
    'FAIL_SYS_TOKEN_EXPIRED': 'platform token expired',
    'FAIL_SYS_ILLEGAL_ACCESS': 'platform rejected access',
    'FAIL_SYS_USER_VALIDATE': 'platform requires verification',
    'RGV587_ERROR': 'platform risk verification',
}


def auth_evidence(http_status, ret, device_id, context):
    codes=[]
    for item in ret if isinstance(ret, list) else []:
        code=str(item).split('::',1)[0]
        code=code if code in MESSAGES else 'UNCLASSIFIED_PLATFORM_ERROR'
        if code not in codes:codes.append(code)
    return {
        'endpoint':'mtop.taobao.idlemessage.pc.login.token/1.0',
        'http_status':http_status,
        'codes':codes,
        'message':[MESSAGES.get(c,'unclassified; raw response omitted') for c in codes],
        'requested_at_utc':datetime.now(timezone.utc).isoformat(),
        'device_summary':hashlib.sha256(device_id.encode()).hexdigest()[:12],
        'context_summary':hashlib.sha256(str(id(context)).encode()).hexdigest()[:12],
        'web_login_evidence':'cookie markers only; user page confirmation required',
    }
