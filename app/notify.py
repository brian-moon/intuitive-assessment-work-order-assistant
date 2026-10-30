"""Alerting: a critical anomaly must reach a person by more than one route.

* **Dashboard**: an alert row the UI shows as a banner until someone acknowledges it.
* **E-mail**: one message per registered recipient whose ``min_severity`` the finding meets, sent through SMTP
  (Gmail with an app password in the demo). Without SMTP settings the channel is recorded as ``disabled``.
  Critical mail is HTML with a red banner and high-importance headers, so it stands out in the inbox. A recipient
  gets one e-mail per asset, metric and severity within the cooldown (live telemetry repeats); the dashboard still
  records every alert. Readings from the telemetry simulator alert on the dashboard only, never by e-mail: a demo
  generator must not fill real inboxes.

Every attempt is written to ``notifications`` with its delivery status, and a dedupe key makes the same finding alert
only once however often the scan runs. The e-mail carries the finding and a link, never an approval action:
approving happens in the app, where roles are checked.
"""
from __future__ import annotations

import html as htmlmod
import logging
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage

from .db import Database
from .faults import Faults

log = logging.getLogger("app.notify")
ORDER = {"warning": 1, "critical": 2}


def smtp_sender(settings) -> Callable[..., None] | None:
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        return None

    def send(to: str, subject: str, body: str, html: str | None = None, severity: str = "warning") -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.smtp_from or settings.smtp_user, to, subject
        if severity == "critical":
            msg["Importance"], msg["X-Priority"], msg["X-MSMail-Priority"] = "High", "1 (Highest)", "High"
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    return send


def render_html(severity: str, subject: str, lines: list[str], url: str) -> str:
    colour, bg = ("#b42318", "#fde8e7") if severity == "critical" else ("#8a5a00", "#fff3dc")
    rows = "".join(f"<p style='margin:6px 0'>{htmlmod.escape(x)}</p>" for x in lines if x)
    return (f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:620px;border:2px solid {colour};border-radius:8px'>"
            f"<div style='background:{colour};color:#fff;padding:14px 18px;font-size:20px;font-weight:700'>"
            f"&#9888; {severity.upper()} ALERT</div>"
            f"<div style='background:{bg};padding:14px 18px;color:#1c2530'><p style='font-size:16px;font-weight:600;"
            f"margin:0 0 8px'>{htmlmod.escape(subject)}</p>{rows}"
            f"<p style='margin:14px 0 0'><a href='{htmlmod.escape(url)}' style='background:{colour};color:#fff;"
            f"padding:8px 14px;border-radius:6px;text-decoration:none'>Open the work-order assistant</a></p></div></div>")


class Notifier:
    def __init__(self, db: Database, faults: Faults, public_url: str, send: Callable[..., None] | None,
                 cooldown_min: int = 15):
        self.db, self.faults, self.public_url, self.send = db, faults, public_url, send
        self.cooldown_min = cooldown_min

    def _cooling(self, recipient: str, f: dict) -> bool:
        """True when this recipient was already e-mailed about this asset, metric and severity within the cooldown."""
        return self.cooldown_min > 0 and self.db.row(
            "SELECT 1 AS x FROM notifications WHERE channel = 'email' AND recipient = %s AND asset_id = %s "
            "AND severity = %s AND dedupe_key LIKE %s AND status IN ('sent', 'pending', 'disabled') "
            "AND created_at > now() - make_interval(mins => %s) LIMIT 1",
            recipient, f["asset_id"], f["severity"], f"anomaly:{f['asset_id']}:{f['metric']}:%", self.cooldown_min) is not None

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
                if ORDER[f["severity"]] < ORDER[r["min_severity"]] or f.get("source") == "simulator":
                    continue
                lines = [f"Asset {f['asset_id']}: {f['metric']} reads {f['value']:g} {f['unit']} at "
                         f"{f['ts']:%Y-%m-%d %H:%M} UTC (threshold {f['threshold']:g}).",
                         "The telemetry is stale; confirm on site." if f["stale"] else "",
                         "Draft and approve the work order in the assistant; nothing is approved from this e-mail."]
                body = f"{subject}\n\n" + "\n".join(x for x in lines if x) + f"\n\n{self.public_url}\n"
                if self._cooling(r["email"], f):
                    continue
                nid = self._claim(key, "email", r["email"], f, subject, "pending")
                if nid is None:
                    continue                               # already handled by this or a concurrent scan
                status, error = "disabled", None
                if self.faults.smtp_unavailable:
                    status, error = "failed", "SMTP server unreachable (injected fault)"
                elif self.send is not None:
                    try:
                        self.send(r["email"], subject, body, render_html(f["severity"], subject, lines, self.public_url),
                                  f["severity"])
                        status = "sent"
                    except Exception as e:   # any failure is recorded and retried; it must not stop other recipients
                        status, error = "failed", f"{type(e).__name__}: {e}"[:300]
                self.db.exec("UPDATE notifications SET status = %s, error = %s WHERE id = %s", status, error, nid)
                raised.append({"channel": "email", "to": r["email"], "status": status, "error": error})
                log.info("alert email %s to %s: %s", status, r["email"], subject)
        return raised
