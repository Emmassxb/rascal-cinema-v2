"""Envío de códigos 2FA. Sustituye a Flask-Mail (acoplado a Flask) por ``smtplib`` estándar."""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol

from rascal.config import Settings

log = logging.getLogger(__name__)


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


@dataclass
class SmtpMailer:
    host: str
    port: int
    sender: str
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    timeout_s: float = 15.0

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as smtp:
            if self.starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(message)


@dataclass
class ConsoleMailer:
    """Desarrollo: escribe el correo en el log y lo guarda en memoria (útil en tests)."""

    outbox: list[tuple[str, str, str]] = field(default_factory=list)

    def send(self, to: str, subject: str, body: str) -> None:
        self.outbox.append((to, subject, body))
        log.warning("[DEV MAIL] to=%s subject=%s\n%s", to, subject, body)


def build_mailer(settings: Settings) -> Mailer:
    if settings.smtp_configured:
        return SmtpMailer(
            host=settings.smtp_host or "",
            port=settings.smtp_port,
            sender=settings.smtp_sender,
            username=settings.smtp_user,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            starttls=settings.smtp_starttls,
        )
    return ConsoleMailer()
