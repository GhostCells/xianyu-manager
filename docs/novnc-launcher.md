# Mac noVNC connection helper

Double-click `scripts/打开云端画面.command`. Keep it beside `novnc_launcher.py`.
It starts a small loopback-only helper at http://127.0.0.1:18768 and opens the page.
Click connect, then open noVNC. The helper does not run manager or access platform
APIs on the Mac. It uses only the existing `xianyu-cloud` SSH alias and forwards
127.0.0.1:18767 to remote 127.0.0.1:6080. No public port or CDP is exposed.

Prerequisites: existing SSH key/known-host trust, Tailscale connected, remote VNC
and noVNC already running. Credentials are entered by the user in noVNC, never
stored by the helper. First SSH trust/key unlock must be done by the user in a
terminal, not disabled. BatchMode prevents an invisible password prompt.

The green status proves HTTP noVNC page connectivity, not VNC authentication,
platform login, WebSocket, or business health. It does not automatically retry
forever. Clicking connect again can replace only this helper's own stale SSH
child; other occupied ports/processes are never killed. Closing the webpage does
not terminate cloud Chrome. Restart the helper after Mac restart using the script.
No launch-at-login service is installed. Demo mode (`--demo`) never starts SSH.

The management page includes a direct noVNC link and an expandable embedded helper.
Embedding is allowed only from `http://127.0.0.1:18766` and the helper's own origin
using CSP frame-ancestors. Other origins (including preview ports) are intentionally
blocked. After updating this script, restart only the Mac helper to load this header;
do not restart cloud Chrome or manager. The helper must already be running before
the embedded controls can load. No cross-origin command API is exposed.

Requests are bound to literal loopback host with Host validation and origin/token
checks for mutations. No configurable commands or destinations are accepted.
Rollback requires stopping only this helper and its own SSH child; it makes no
server-side configuration changes.

Readiness probes allow up to 8 seconds for a noVNC HTTP response over the private
tunnel. A connection attempt remains bounded (approximately 40 seconds including
the final probe); a slow response must not be mistaken for a dead tunnel.
