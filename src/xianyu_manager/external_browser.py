"""Attach to an already-owned Chrome; never launch or close its browser/context."""
from pathlib import Path
from .profile_lock import ProfileOwnerLock

CDP_URL = 'http://127.0.0.1:9222'


def validate_cdp_url(value):
    if value and value != CDP_URL:
        raise ValueError('CDP_ENDPOINT_MUST_BE_FIXED_RUNTIME_LOOPBACK')
    return value


def validate_browser_arguments(arguments, profile):
    expected = str(Path(profile).resolve())
    profiles = [a.split('=', 1)[1] for a in arguments if a.startswith('--user-data-dir=')]
    if profiles != [expected]:
        raise RuntimeError('CDP_PROFILE_IDENTITY_MISMATCH')
    for flag, value in [('--remote-debugging-address=', '127.0.0.1'),
                        ('--remote-debugging-port=', '9222')]:
        if [a[len(flag):] for a in arguments if a.startswith(flag)] != [value]:
            raise RuntimeError('CDP_LISTENER_IDENTITY_MISMATCH')
    if any(a.split('=', 1)[0] in {'--no-sandbox', '--disable-setuid-sandbox',
                                '--incognito', '--guest'} for a in arguments):
        raise RuntimeError('CDP_UNSAFE_BROWSER_ARGUMENTS')


class ExternalBrowserConnection:
    def __init__(self, profile, endpoint, account_id):
        validate_cdp_url(endpoint)
        if endpoint != CDP_URL or account_id != 2:
            raise ValueError('EXTERNAL_BROWSER_ACCOUNT_NOT_APPROVED')
        self.profile = Path(profile)
        # Client lock is separate from the daemon's exclusive profile-owner lock.
        self.lock = ProfileOwnerLock(self.profile.with_name(self.profile.name+'.cdp-client'))
        self.endpoint = endpoint
        self.playwright = None
        self.browser = None

    async def connect(self, start_playwright):
        if self.playwright is not None:
            raise RuntimeError('CDP_ALREADY_ATTACHED')
        self.lock.acquire()
        try:
            self.playwright = await start_playwright()
            self.browser = await self.playwright.chromium.connect_over_cdp(
                self.endpoint, timeout=15000, no_defaults=True)
            channel = await self.browser.new_browser_cdp_session()
            try:
                response = await channel.send('Browser.getBrowserCommandLine')
                validate_browser_arguments(response.get('arguments', []), self.profile)
            finally:
                await channel.detach()
            contexts = self.browser.contexts
            if len(contexts) != 1:
                raise RuntimeError('CDP_REQUIRES_SINGLE_PERSISTENT_CONTEXT')
            context = contexts[0]
            pages = [p for p in context.pages if not p.is_closed()]
            if not pages:
                raise RuntimeError('CDP_PERSISTENT_PAGE_MISSING')
            page = next((p for p in pages if p.url.startswith('https://www.goofish.com/im')), pages[0])
            return context, page
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self):
        driver, self.playwright = self.playwright, None
        self.browser = None
        try:
            if driver is not None:
                # Stop this client's driver transport. Never Browser.close or Context.close.
                await driver.stop()
        finally:
            self.lock.release()
