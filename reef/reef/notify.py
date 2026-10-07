"""Delivers digests and alerts by Telegram and/or e-mail. Both are optional; reports are always saved to disk."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import Config

log = logging.getLogger("reef.notify")


class Notifier:
    def __init__(self, cfg: Config, client: httpx.Client | None = None):
        self.cfg = cfg
        self._http = client or httpx.Client(timeout=30)
        self.sent: list[tuple[str, str]] = []  # (subject, body), kept for tests and the status command

    def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))
        delivered = False
        if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            delivered |= self._telegram(f"{subject}\n\n{body}")
        if self.cfg.smtp_host and self.cfg.report_email_to:
            delivered |= self._email(subject, body)
        if not delivered:
            log.info("notification (no channel configured): %s\n%s", subject, body)

    def _telegram(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        ok = True
        for i in range(0, len(text), 3900):  # Telegram caps messages at 4096 chars
            try:
                resp = self._http.post(url, json={"chat_id": self.cfg.telegram_chat_id, "text": text[i:i + 3900],
                                                  "disable_web_page_preview": True})
                ok &= resp.status_code == 200
            except httpx.HTTPError as exc:
                log.warning("telegram send failed: %s", exc)
                ok = False
        return ok

    def _email(self, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["Subject"] = f"[Reef] {subject}"
        msg["From"] = self.cfg.smtp_user or self.cfg.report_email_to
        msg["To"] = self.cfg.report_email_to
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                if self.cfg.smtp_user:
                    smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
                smtp.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("email send failed: %s", exc)
            return False
