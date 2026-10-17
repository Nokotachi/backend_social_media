import asyncio
import logging
import smtplib
import sys
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Protocol

from user_service.settings import Settings, get_settings

logger = logging.getLogger("user_service.email")

_SUBJECTS = {
    "verify": "Код подтверждения регистрации",
    "reset": "Код для восстановления пароля",
    "change": "Код для смены пароля",
}


def _body(kind: str, code: str, ttl_minutes: int) -> str:
    return (
        f"{_SUBJECTS[kind]}: {code}\n\n"
        f"Код действует {ttl_minutes} минут. Если вы ничего не запрашивали, "
        "просто проигнорируйте это письмо."
    )


class EmailDeliveryFailed(Exception): ...


class EmailSender(Protocol):
    async def send_verification_code(self, *, to_email: str, code: str) -> None: ...

    async def send_password_reset_code(self, *, to_email: str, code: str) -> None: ...

    async def send_password_change_code(self, *, to_email: str, code: str) -> None: ...


class _BaseSender:
    async def _send(self, kind: str, to_email: str, code: str) -> None:
        raise NotImplementedError

    async def send_verification_code(self, *, to_email: str, code: str) -> None:
        await self._send("verify", to_email, code)

    async def send_password_reset_code(self, *, to_email: str, code: str) -> None:
        await self._send("reset", to_email, code)

    async def send_password_change_code(self, *, to_email: str, code: str) -> None:
        await self._send("change", to_email, code)


class ConsoleEmailSender(_BaseSender):
    async def _send(self, kind: str, to_email: str, code: str) -> None:
        logger.info("[STUB EMAIL] To: %s | %s: %s", to_email, _SUBJECTS[kind], code)


class SmtpEmailSender(_BaseSender):
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def _connect(self) -> smtplib.SMTP:
        s = self._s
        if s.smtp_ssl:
            return smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
        smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
        if s.smtp_starttls:
            smtp.starttls()
        return smtp

    def _send_sync(self, msg: EmailMessage) -> None:
        with self._connect() as smtp:
            if self._s.smtp_username:
                smtp.login(self._s.smtp_username, self._s.smtp_password)
            smtp.send_message(msg)

    def build_message(self, *, to_email: str, subject: str, body: str) -> EmailMessage:
        name, addr = parseaddr(self._s.smtp_from)
        if self._s.smtp_username and "@" in self._s.smtp_username:
            addr = self._s.smtp_username
        msg = EmailMessage()
        msg["From"] = Address(display_name=name, addr_spec=addr) if name else addr
        msg["To"] = to_email
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=addr.rpartition("@")[2] or None)
        msg.set_content(body)
        return msg

    async def send(self, msg: EmailMessage) -> None:
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except (smtplib.SMTPException, OSError) as exc:
            logger.error(
                "SMTP %s:%s не принял письмо для %s: %r",
                self._s.smtp_host,
                self._s.smtp_port,
                msg["To"],
                exc,
            )
            raise EmailDeliveryFailed() from exc

    async def _send(self, kind: str, to_email: str, code: str) -> None:
        msg = self.build_message(
            to_email=to_email,
            subject=_SUBJECTS[kind],
            body=_body(kind, code, self._s.email_code_ttl_minutes),
        )
        await self.send(msg)


def get_email_sender() -> EmailSender:
    settings = get_settings()
    if settings.email_backend == "smtp":
        return SmtpEmailSender(settings)
    return ConsoleEmailSender()


async def _send_test(to_email: str) -> None:
    settings = get_settings()
    if settings.email_backend != "smtp":
        print("EMAIL_BACKEND не smtp — письмо было бы только в логе")
        return
    sender = SmtpEmailSender(settings)
    msg = sender.build_message(
        to_email=to_email,
        subject="Проверка почты Social Media",
        body="Если вы видите это письмо, отправка кодов настроена правильно.",
    )
    await sender.send(msg)
    print(f"Отправлено на {to_email} через {settings.smtp_host}:{settings.smtp_port}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 2:
        sys.exit("usage: python -m user_service.core.email you@example.com")
    try:
        asyncio.run(_send_test(sys.argv[1]))
    except EmailDeliveryFailed as exc:
        sys.exit(f"Не отправлено: {exc.__cause__!r}")
