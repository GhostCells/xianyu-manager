"""Linux account-2 browser owner. No database, LLM key, token or delivery tasks."""
import asyncio
import os
from pathlib import Path
import signal
import sys

from xianyu_manager.browser_launch import sandbox_options
from xianyu_manager.profile_lock import ProfileOwnerLock
from xianyu_manager.runtime_policy import RuntimePolicy

PROFILE = Path('/var/lib/xianyu-runtime/candidate-20260907/data/browser-profiles/google-chrome/account-2')
EXECUTABLE = Path('/usr/bin/google-chrome')


async def own_browser():
    if sys.platform != 'linux' or os.getuid() == 0:
        raise RuntimeError('INDEPENDENT_CHROME_REQUIRES_UNPRIVILEGED_LINUX')
    if not PROFILE.is_dir() or PROFILE.is_symlink() or not EXECUTABLE.is_file():
        raise RuntimeError('EXISTING_PROFILE_AND_CHROME_REQUIRED')
    policy = RuntimePolicy(account_id=2, login_authorized=True,
                           egress_status_path=Path('/run/xianyu-egress/status.json'))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    with ProfileOwnerLock(PROFILE):
        for _ in range(60):
            if stop.is_set():return
            if policy.egress_status()['ready']:break
            await asyncio.sleep(1)
        else:raise RuntimeError('TRUSTED_EGRESS_STARTUP_TIMEOUT')
        policy.require_login(2)
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            context = await driver.chromium.launch_persistent_context(
                **sandbox_options(), user_data_dir=str(PROFILE),
                executable_path=str(EXECUTABLE), headless=False, no_viewport=True,
                args=['--start-maximized', '--no-first-run', '--no-default-browser-check',
                      '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9222'])
            context.on('close', lambda _: stop.set())
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                if page.url == 'about:blank':
                    try:await page.goto('https://www.goofish.com/im', timeout=60000, wait_until='domcontentloaded')
                    except Exception:pass
                # Only the owner lifecycle closes Chrome. Egress revocation remains
                # enforced by the existing expiring nft lease outside this process.
                await stop.wait()
            finally:
                await context.close()


if __name__ == '__main__':
    try:asyncio.run(own_browser())
    except Exception as exc:
        print('INDEPENDENT_CHROME_FAILED:'+type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
