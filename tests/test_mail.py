"""Mail delivery and the alert-recipient set-up: the SMTP client against a fake server (nothing is sent), and the
dashboard data behind the "Where should critical alerts go?" prompt and the test-inbox link."""
from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

import pytest
from conftest import auth

from app import notify


class FakeSMTP:
    """Records what the client does; ``tls`` says whether the server offers STARTTLS."""
    sessions: ClassVar[list[dict]] = []
    tls = False

    def __init__(self, host, port, timeout):
        self.log = {"host": host, "port": port, "starttls": False, "login": None, "messages": []}
        FakeSMTP.sessions.append(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def has_extn(self, name):
        return name == "starttls" and FakeSMTP.tls

    def starttls(self, context=None):
        self.log["starttls"] = True

    def login(self, user, password):
        self.log["login"] = user

    def send_message(self, msg):
        self.log["messages"].append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.sessions = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_no_host_means_no_sender(settings):
    assert notify.smtp_sender(replace(settings, smtp_host="")) is None


def test_test_inbox_needs_no_login_and_no_tls(settings, fake_smtp):
    fake_smtp.tls = False
    send = notify.smtp_sender(replace(settings, smtp_host="mailpit", smtp_port=1025, smtp_user="", smtp_password="",
                                      smtp_from="alerts@workorder-assistant.local"))
    send("ops@x.org", "[CRITICAL] P-101", "body", "<p>html</p>", "critical")
    s = fake_smtp.sessions[-1]
    assert (s["host"], s["port"], s["starttls"], s["login"]) == ("mailpit", 1025, False, None)
    m = s["messages"][0]
    assert m["From"] == "alerts@workorder-assistant.local" and m["Importance"] == "High"
    assert m.get_body(("html",)).get_content().strip() == "<p>html</p>"


def test_real_server_uses_tls_and_signs_in(settings, fake_smtp):
    fake_smtp.tls = True
    send = notify.smtp_sender(replace(settings, smtp_host="smtp.example.org", smtp_port=587, smtp_user="me@example.org",
                                      smtp_password="app-password", smtp_from="me@example.org"))
    send("ops@x.org", "[WARNING] H-501", "body")
    s = fake_smtp.sessions[-1]
    assert s["starttls"] is True and s["login"] == "me@example.org" and "Importance" not in s["messages"][0]


def test_a_real_server_without_tls_is_refused_before_signing_in(settings, fake_smtp):
    """A server that does not offer STARTTLS (or a man in the middle that strips it) never gets the password."""
    fake_smtp.tls = False
    send = notify.smtp_sender(replace(settings, smtp_host="smtp.example.org", smtp_port=587, smtp_user="me@example.org",
                                      smtp_password="app-password", smtp_from="me@example.org"))
    with pytest.raises(notify.smtplib.SMTPNotSupportedError):
        send("ops@x.org", "[CRITICAL] P-101", "body")
    s = fake_smtp.sessions[-1]
    assert s["login"] is None and s["messages"] == []


def test_dashboard_tells_whether_anyone_receives_alerts(make_client, settings):
    plain = make_client()
    d = plain.get("/api/dashboard", headers=auth(plain, "viewer")).json()
    assert d["alert_recipients"] == 0 and d["mail_inbox_url"] is None
    c = make_client(s=replace(settings, smtp_host="mailpit", mail_inbox_url="http://localhost:8025"))
    admin = auth(c, "admin")
    assert c.get("/api/dashboard", headers=admin).json()["mail_inbox_url"] == "http://localhost:8025"
    c.post("/api/admin/recipients", headers=admin, json={"name": "Ops", "email": "ops@x.org"})
    viewer = c.get("/api/dashboard", headers=auth(c, "viewer")).json()
    assert viewer["alert_recipients"] == 1 and "ops@x.org" not in str(viewer)


def test_inbox_link_only_while_mail_goes_to_the_test_inbox(make_client, settings):
    c = make_client(s=replace(settings, smtp_host="smtp.example.org", mail_inbox_url="http://localhost:8025"))
    assert c.get("/api/dashboard", headers=auth(c, "viewer")).json()["mail_inbox_url"] is None
