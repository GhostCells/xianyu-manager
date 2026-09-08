from xianyu_manager.message_auth_diagnostics import auth_evidence


def test_secrets_and_raw_messages_never_escape():
    result=auth_evidence(200,['FAIL_SYS_TOKEN_EXPIRED::secret-cookie', 'raw-secret-token'],'private-device-id',object())
    text=str(result)
    assert 'secret-cookie' not in text and 'raw-secret-token' not in text and 'private-device-id' not in text
    assert result['codes']==['FAIL_SYS_TOKEN_EXPIRED','UNCLASSIFIED_PLATFORM_ERROR']


def test_same_context_and_device_can_be_correlated():
    context=object()
    a=auth_evidence(200,['SUCCESS::private'],'device',context)
    b=auth_evidence(403,['FAIL_SYS_ILLEGAL_ACCESS::private'],'device',context)
    assert a['context_summary']==b['context_summary']
    assert a['device_summary']==b['device_summary']
    assert b['http_status']==403
