"""Offline, headed, synthetic Persistent Context probe for the candidate unit."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from playwright.async_api import async_playwright
from xianyu_manager.browser_launch import sandbox_options
from xianyu_manager.profile_lock import ProfileOwnerLock


async def main():
    assert os.getuid() != 0
    assert os.environ['XIANYU_MANAGER_PREPARE_MODE'] == 'true'
    assert os.environ['XIANYU_MANAGER_LOGIN_AUTHORIZED'] == 'false'
    profile = Path(tempfile.mkdtemp(prefix='sandbox-proof-', dir='/var/lib/xianyu-runtime'))
    result = {'uid': os.getuid(), 'headless': False, 'sandbox_options': sandbox_options()}
    lock = ProfileOwnerLock(profile)
    lock.acquire()
    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(profile), executable_path='/usr/bin/google-chrome-stable',
                headless=False, **sandbox_options(),
                args=['--no-first-run', '--disable-background-networking', '--disable-component-update', '--disable-sync'],
            )
            try:
                second = ProfileOwnerLock(profile)
                try:
                    second.acquire()
                except Exception:
                    result['second_owner_rejected'] = True
                else:
                    second.release()
                    raise AssertionError('SECOND_OWNER_ACCEPTED')
                page = context.pages[0]
                await page.set_content('<button onclick="this.textContent=\'done\'">test</button>')
                await page.get_by_role('button').click()
                assert await page.get_by_role('button').inner_text() == 'done'
                result['interaction_passed'] = True
                await page.goto('chrome://sandbox')
                text = await page.locator('body').inner_text()
                result['sandbox_status'] = text
                assert 'Seccomp-BPF sandbox' in text and 'Yes' in text
                processes=[]
                for proc in Path('/proc').iterdir():
                    if not proc.name.isdigit(): continue
                    try:
                        if proc.stat().st_uid != os.getuid(): continue
                        args=(proc/'cmdline').read_bytes().split(b'\0')
                        if b'/opt/google/chrome/chrome' not in args: continue
                        assert b'--no-sandbox' not in args and b'--disable-setuid-sandbox' not in args
                        processes.append({'pid':int(proc.name), 'netns':os.stat(proc/'ns/net').st_ino,
                                          'type':next((a.decode() for a in args if a.startswith(b'--type=')),'browser')})
                    except (FileNotFoundError, PermissionError): continue
                assert processes
                result['processes']=processes
            finally:
                await context.close()
    finally:
        lock.release()
    again=ProfileOwnerLock(profile);again.acquire();again.release()
    result['lock_released']=True
    Path('/var/lib/xianyu-runtime/browser-sandbox-proof.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)


asyncio.run(main())
