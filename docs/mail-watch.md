# Account 2 independent mail monitoring

This monitor is separate from manager and Chrome. It does not restart services,
call platform endpoints, renew exit approvals, or recover orders. Existing manager
WebSocket backoff and trusted-egress recovery remain authoritative. Manual login
or root approval requirements are reported, never automatically bypassed.

Install `scripts/mail_watch.py` at `/opt/xianyu-mail-watch/mail_watch.py`, root owned.
Install the adjacent service/timer examples as `xianyu-mail-watch.service/timer`.
SMTP credentials are root-owned mode 0600 at `/etc/xianyu-notifications/smtp.json`;
directory mode 0700. Never commit credentials. Only SMTP SSL 465 is supported.
Monitoring uses host management networking for SMTP, not the protected platform
runtime. All business calls retain the existing account exit. The monitor reads
only local health/delivery GETs; raw responses are neither stored nor emailed.

Check every 30 seconds. An incident lasting at least 60 seconds causes one alert;
two subsequent healthy samples cause one recovery notification. SMTP failures
have at most three attempts, separated by 60 then 300 seconds. Ambiguous SMTP
acceptance may produce a duplicate email, never duplicate delivery. Incident state
is persistent and flock prevents concurrent executions. Healthy startup is silent.
Check `journalctl -u xianyu-mail-watch.service` for sanitized status codes.

Send one explicitly approved test with `sudo python3 -I /opt/xianyu-mail-watch/mail_watch.py --test-email`.
SMTP acceptance is not proof the recipient received the mail; ask the recipient.
This is status monitoring, not proof an LLM call or platform delivery succeeds.
Host failure or complete outbound outage can prevent mail: an off-host heartbeat
would be needed to detect that failure independently. This monitor does not add it.

Rollback: disable/stop only `xianyu-mail-watch.timer`; no business restart required.
WindowsNotifier remains legacy code but is inert on Linux; mail does not depend on it.
