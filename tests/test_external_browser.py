import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from xianyu_manager.external_browser import (CDP_URL, ExternalBrowserConnection,
    validate_cdp_url, validate_browser_arguments)
from xianyu_manager.profile_lock import ProfileOwnerLock, ProfileOwnerError
from xianyu_manager.session import BrowserSessionManager
from xianyu_manager.database import Database


def fixture(profile):
    args = ['--user-data-dir='+str(profile.resolve()), '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=9222']
    page = SimpleNamespace(url='https://www.goofish.com/im', is_closed=lambda:False)
    context = SimpleNamespace(pages=[page], close=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value={'arguments':args}), detach=AsyncMock())
    browser = SimpleNamespace(contexts=[context], close=AsyncMock(),
                              new_browser_cdp_session=AsyncMock(return_value=channel))
    driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)),
                             stop=AsyncMock())
    return driver, browser, context, args


@pytest.mark.parametrize('url',['http://0.0.0.0:9222','http://localhost:9222',
    'https://remote.example:9222','http://127.0.0.1:9222/path','http://127.0.0.1:9999'])
def test_only_runtime_loopback_endpoint(url):
    with pytest.raises(ValueError):validate_cdp_url(url)


@pytest.mark.parametrize('flag',['--no-sandbox','--guest','--incognito','--disable-setuid-sandbox'])
def test_unsafe_browser_rejected(tmp_path, flag):
    _,_,_,args=fixture(tmp_path)
    with pytest.raises(RuntimeError):validate_browser_arguments(args+[flag],tmp_path)


def test_profile_mismatch_and_duplicate_flags_rejected(tmp_path):
    _,_,_,args=fixture(tmp_path)
    with pytest.raises(RuntimeError):validate_browser_arguments(args,tmp_path/'other')
    with pytest.raises(RuntimeError):validate_browser_arguments(args+['--remote-debugging-port=9222'],tmp_path)


def test_disconnect_does_not_close_browser_or_context(tmp_path):
    driver,browser,context,_=fixture(tmp_path/'account-2')
    async def run():
        conn=ExternalBrowserConnection(tmp_path/'account-2',CDP_URL,2)
        owner=ProfileOwnerLock(tmp_path/'account-2');owner.acquire()
        try:
            ctx,page=await conn.connect(AsyncMock(return_value=driver))
            assert ctx is context
            with pytest.raises(ProfileOwnerError):
                second=ExternalBrowserConnection(tmp_path/'account-2',CDP_URL,2)
                await second.connect(AsyncMock(return_value=driver))
            await conn.disconnect()
        finally:owner.release()
        context.close.assert_not_called();browser.close.assert_not_called();driver.stop.assert_awaited_once()
    asyncio.run(run())


def test_failed_attach_never_launches_or_closes_external_browser(tmp_path):
    driver,browser,context,args=fixture(tmp_path)
    args.append('--guest')
    async def run():
        conn=ExternalBrowserConnection(tmp_path,CDP_URL,2)
        with pytest.raises(RuntimeError):await conn.connect(AsyncMock(return_value=driver))
        assert conn.playwright is None
        context.close.assert_not_called();browser.close.assert_not_called();driver.stop.assert_awaited_once()
    asyncio.run(run())


def test_manager_shutdown_only_detaches(tmp_path):
    manager=BrowserSessionManager(tmp_path/'profiles',None,Database(tmp_path/'db'))
    driver,browser,context,_=fixture(tmp_path/'account-2')
    async def run():
        conn=ExternalBrowserConnection(tmp_path/'account-2',CDP_URL,2)
        manager._context,manager._page=await conn.connect(AsyncMock(return_value=driver))
        manager._external_connection=conn
        await manager.shutdown()
        assert manager._context is None
        context.close.assert_not_called();browser.close.assert_not_called()
    asyncio.run(run())


def test_external_wrong_account_rejected(tmp_path):
    with pytest.raises(ValueError):ExternalBrowserConnection(tmp_path,CDP_URL,1)


def test_egress_attests_both_units_only_when_root_binding_valid(tmp_path,monkeypatch):
    from xianyu_manager import egress_control as c
    p=tmp_path/'binding.json';p.touch();monkeypatch.setattr(c,'CHROME_BINDING',p)
    monkeypatch.setattr(c,'read_root_json',lambda _: {'schema_version':1,'account_id':2,'unit':c.CHROME_UNIT})
    calls=[]
    def verify(unit,required=True):calls.append((unit,required));return True
    monkeypatch.setattr(c,'_unit_runtime_verified',verify)
    assert c.runtime_verified()
    assert calls==[(c.CHROME_UNIT,True),(c.UNIT,False)]
    monkeypatch.setattr(c,'read_root_json',lambda _: {'account_id':1})
    assert not c.runtime_verified()


def test_failed_chrome_attestation_blocks_egress(tmp_path,monkeypatch):
    from xianyu_manager import egress_control as c
    p=tmp_path/'binding.json';p.touch();monkeypatch.setattr(c,'CHROME_BINDING',p)
    monkeypatch.setattr(c,'read_root_json',lambda _: {'schema_version':1,'account_id':2,'unit':c.CHROME_UNIT})
    monkeypatch.setattr(c,'_unit_runtime_verified',lambda *a,**k:False)
    assert not c.runtime_verified()


def test_external_recovery_does_not_change_switches():
    from xianyu_manager.runtime_policy import RuntimePolicy
    from xianyu_manager.egress_recovery import recovery_allowed
    p=RuntimePolicy(account_id=2,resident_reply=True,mvp_fulfillment=True,catalog_delivery=True,
                    order_cutoff_at='2026-09-08T00:51:38.276470+00:00')
    assert not recovery_allowed(p,delivery_enabled=True)
    assert recovery_allowed(p,delivery_enabled=True,external_browser=True)
    assert not p.order_recovery_enabled


@pytest.mark.parametrize('required,group,expected',[
    (False,'',True), (True,'',False),
    (False,'/system.slice/unexpected.service',False),
    (False,'/system.slice/xianyu-isolated-prepare.service',True)])
def test_stopped_manager_attestation(tmp_path,monkeypatch,required,group,expected):
    import pwd
    from xianyu_manager import egress_control as c
    real_path=Path
    ns=tmp_path/'run/netns'/c.NAMESPACE
    ns.parent.mkdir(parents=True);ns.touch()
    cg=tmp_path/'sys/fs/cgroup/system.slice/xianyu-isolated-prepare.service'
    cg.mkdir(parents=True);(cg/'cgroup.procs').write_text('')
    monkeypatch.setattr(c,'Path',lambda value:real_path(tmp_path/str(value).lstrip('/')))
    monkeypatch.setattr(c,'run',lambda *a:f'MainPID=0\nControlGroup={group}')
    monkeypatch.setattr(pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=999 if name=='xianyu-runtime' else 1000))
    assert c._unit_runtime_verified(c.UNIT,required=required) is expected


@pytest.mark.parametrize('trusted_uid',[True,False])
def test_stopping_manager_remaining_processes_checked(tmp_path,monkeypatch,trusted_uid):
    import os
    import pwd
    from xianyu_manager import egress_control as c
    ns=tmp_path/'run/netns'/c.NAMESPACE
    ns.parent.mkdir(parents=True);ns.touch()
    cg=tmp_path/'sys/fs/cgroup/system.slice'/c.UNIT
    cg.mkdir(parents=True);(cg/'cgroup.procs').write_text('42')
    proc=tmp_path/'proc/42/ns';proc.mkdir(parents=True)
    (proc/'net').symlink_to(ns)
    monkeypatch.setattr(c,'Path',lambda value:Path(tmp_path/str(value).lstrip('/')))
    monkeypatch.setattr(c,'run',lambda *a:f'MainPID=0\nControlGroup=/system.slice/{c.UNIT}')
    uid=os.getuid()
    monkeypatch.setattr(pwd,'getpwnam',lambda name:SimpleNamespace(
        pw_uid=(uid if trusted_uid else uid+1) if name=='xianyu-runtime' else uid+2))
    assert c._unit_runtime_verified(c.UNIT,required=False) is trusted_uid
