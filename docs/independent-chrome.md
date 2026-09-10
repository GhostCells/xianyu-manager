# Account 2 independent Chrome — migration candidate

Status: opt-in implementation, not evidence of production activation. Default remains manager-owned Chrome.

## Ownership

`xianyu-chrome-account2.service` alone owns the existing persistent Profile and its owner lock. Manager connects to `http://127.0.0.1:9222` inside `xianyu-business` and takes a separate client lock. Disconnect/shutdown never closes the external context. Missing CDP fails without launching a replacement browser. noVNC is only a viewer.

No database migration, cutoff change, device ID change, order replay, scheduler or recovery enablement is included. Existing business switches remain authoritative.

If Chrome omits `--enable-automation`, command-line attestation falls back to the exact loopback listener socket inode, same runtime UID and independent-owner cgroup in procfs. Profile and sandbox flags are still checked; an unknown owner is refused. No automation flag is added to the production browser.

## Required deployment gates

- Explicit production cutover approval, including protected CDP control access. Never forward CDP to Mac, host/public interfaces or the internet.
- Verify cloud Playwright supports `connect_over_cdp(no_defaults=True)` before switching.
- Verify current nft lease gates **all** business traffic, including established connections, and expires closed. Do not use this mode if established traffic bypasses the lease.
- Record service and Chrome PIDs, Profile inode, runtime account, WebSocket status, business switches, cutoff and deployment hashes; do not record credentials.
- Back up code, units and environment files with restricted permissions; use a consistent DB backup. Do not overwrite live order data during rollback.

## Controlled migration

1. Stage reviewed code and the service template. Do not start another Chrome.
2. Stop manager once, wait for original Chrome to exit normally and the Profile lock to release. Preserve Profile and all data. Never delete a live lock to force startup.
3. Install the reviewed updater code and root-owned `0600` `/etc/xianyu-egress/independent-chrome.json` with exactly `{"schema_version":1,"account_id":2,"unit":"xianyu-chrome-account2.service"}`. No trust approval or lease duration changes.
4. Start the independent owner and updater. The owner waits at most 60 seconds for trustworthy egress. The updater attests the owner unit and any running manager processes. A stopped manager is permitted only in this root-authorized mode.
5. Add `XIANYU_MANAGER_BROWSER_CDP_URL=http://127.0.0.1:9222` to a dedicated manager drop-in; retain existing account and capability settings. The drop-in uses `Wants=` and `After=xianyu-chrome-account2.service` (not `PartOf=`), and an additional `ExecStartPre` running `scripts/wait_independent_chrome.py`. This bounded loopback readiness probe avoids racing the browser at boot; the actual CDP attachment still verifies identity. Enable the independent owner at boot. Start manager. Inspect listener binding inside the namespace and prove no outside exposure.
6. Check original Profile identity, single owner, actual login status, token and listening WebSocket. User performs official verification only if actually required.
7. Verify one approved manager restart/disconnect leaves Chrome PID and Profile unchanged. Observe bounded recovery and unchanged business flags. Do not create orders, replay events or scan history.

## Rollback

Stop manager; stop independent Chrome normally and wait for its owner lock to release. Restore prior updater/unit/environment configuration, removing only this opt-in root binding and CDP setting. Start the prior manager-owned mode with the **same** Profile. Never run both owners, restore an old DB over new orders, change cutoff, clear cookies or weaken egress validation.

## Verification limits

Mocked tests establish call boundaries, identity checks and lock behavior, not a real persistent login guarantee. Production cutover must still prove CDP detach survival, actual network lease enforcement and stable WebSocket recovery. Independent Chrome reduces deployment-induced browser shutdowns; it cannot promise the platform never requests verification.
