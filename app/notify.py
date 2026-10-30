"""Alerting: a critical anomaly must reach a person by more than one route.

* **Dashboard**: an alert row the UI shows as a banner until someone acknowledges it.
* **E-mail**: one message per registered recipient whose ``min_severity`` the finding meets, sent through SMTP
  (Gmail with an app password in the demo). Without SMTP settings the channel is recorded as ``disabled``.

Every attempt is written to ``notifications`` with its delivery status, and a dedupe key makes the same finding alert
only once however often the scan runs. The e-mail carries the finding and a link, never an approval action:
approving happens in the app, where roles are checked.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage

from .db import Database
from .faults import Faults

log = logging.getLogger("app.notify")
ORDER = {"warning": 1, "critical": 2}


def smtp_sender(settings) -> Callable[[str, str, str], None] | None:
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        return None

    def send(to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.smtp_from or settings.smtp_user, to, subject
        msg.set_content(body)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    return send


class Notifier:
    def __init__(self, db: Database, faults: Faults, public_url: str, send: Callable[[str, str, str], None] | None):
        self.db, self.faults, self.public_url, self.send = db, faults, public_url, send

    def _claim(self, key, channel, recipient, f, subject, status) -> int | None:
        """Claim the notification row before delivering; only the caller that gets the row may deliver, so concurrent
        scans send at most one e-mail per recipient. A delivery that failed, or a claim older than 10 minutes (a
        crashed sender), can be claimed again by a later scan, so an alert is never lost to one bad attempt."""
        r = self.db.row(
            "INSERT INTO notifications (dedupe_key, channel, recipient, severity, asset_id, subject, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key, channel, recipient) DO UPDATE "
            "SET status = EXCLUDED.status, error = NULL, claimed_at = now() "
            "WHERE notifications.channel = 'email' AND (notifications.status = 'failed' OR "
            "(notifications.status = 'pending' AND notifications.claimed_at < now() - interval '10 minutes')) "
            "RETURNING id", key, channel, recipient, f["severity"], f["asset_id"], subject, status)
        return r["id"] if r else None

    def alert(self, findings: list[dict]) -> list[dict]:
        """Raise alerts for warning/critical findings. Returns what was newly recorded."""
        recipients = self.db.rows("SELECT name, email, min_severity FROM alert_recipients ORDER BY id")
        raised = []
        for f in findings:
            if f["severity"] not in ORDER:
                continue
            key = f"anomaly:{f['asset_id']}:{f['metric']}:{f['ts'].isoformat()}:{f['severity']}"
            subject = (f"[{f['severity'].upper()}] {f['asset_id']} {f['metric']} = {f['value']:g} {f['unit']}"
                       + (" (stale data)" if f["stale"] else ""))
            if f["severity"] == "critical" and self._claim(key, "dashboard", "", f, subject, "shown"):
                raised.append({"channel": "dashboard", "subject": subject})
            for r in recipients:
                if ORDER[f["severity"]] < ORDER[r["min_severity"]]:
                    continue
                body = (f"{subject}\n\nAsset {f['asset_id']}: {f['metric']} reads {f['value']:g} {f['unit']} at "
                        f"{f['ts'].isoformat()} (threshold {f['threshold']:g}).\n"
                        f"{'The telemetry is stale; confirm on site.' if f['stale'] else ''}\n\n"
                        f"Open the work-order assistant to draft and approve a work order: {self.public_url}\n")
                nid = self._claim(key, "email", r["email"], f, subject, "pending")
                if nid is None:
                    continue                               # already handled by this or a concurrent scan
                status, error = "disabled", None
                if self.faults.smtp_unavailable:
                    status, error = "failed", "SMTP server unreachable (injected fault)"
                elif self.send is not None:
                    try:
                        self.send(r["email"], subject, body)
                        status = "sent"
                    except Exception as e:   # any failure is recorded and retried; it must not stop other recipients
                        status, error = "failed", f"{type(e).__name__}: {e}"[:300]
                self.db.exec("UPDATE notifications SET status = %s, error = %s WHERE id = %s", status, error, nid)
                raised.append({"channel": "email", "to": r["email"], "status": status, "error": error})
                log.info("alert email %s to %s: %s", status, r["email"], subject)
        return raised
