"""
Email tool — read via IMAP, send via SMTP.
Configure with env vars:
  EMAIL_HOST      (e.g. imap.gmail.com / smtp.gmail.com)
  EMAIL_USER      your@email.com
  EMAIL_PASSWORD  app-password (for Gmail: https://myaccount.google.com/apppasswords)
  SMTP_HOST       optional, defaults to EMAIL_HOST
  SMTP_PORT       optional, defaults to 587
"""
import asyncio
import email
import imaplib
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional


def _get_cfg() -> dict:
    return {
        "imap_host": os.getenv("EMAIL_HOST", "imap.gmail.com"),
        "smtp_host": os.getenv("SMTP_HOST", os.getenv("EMAIL_HOST", "smtp.gmail.com")),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("EMAIL_USER", ""),
        "password": os.getenv("EMAIL_PASSWORD", ""),
    }


def _configured() -> bool:
    cfg = _get_cfg()
    return bool(cfg["user"] and cfg["password"])


def _read_emails_sync(n: int = 5, folder: str = "INBOX") -> list[dict]:
    cfg = _get_cfg()
    results = []
    try:
        mail = imaplib.IMAP4_SSL(cfg["imap_host"])
        mail.login(cfg["user"], cfg["password"])
        mail.select(folder)
        _, data = mail.search(None, "ALL")
        ids = data[0].split()
        for uid in reversed(ids[-n:]):
            _, msg_data = mail.fetch(uid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="replace")
            results.append({
                "from": msg.get("From", ""),
                "subject": msg.get("Subject", ""),
                "date": msg.get("Date", ""),
                "body": body[:800],
            })
        mail.logout()
    except Exception as e:
        return [{"error": str(e)}]
    return list(reversed(results))


def _send_email_sync(to: str, subject: str, body: str) -> str:
    cfg = _get_cfg()
    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["user"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["user"], to, msg.as_string())
        return f"Email sent to {to}"
    except Exception as e:
        return f"Failed to send email: {e}"


async def read_emails(n: int = 5) -> str:
    if not _configured():
        return "Email ikke konfigureret. Sæt EMAIL_HOST, EMAIL_USER og EMAIL_PASSWORD i Railway Variables."
    emails = await asyncio.get_event_loop().run_in_executor(None, _read_emails_sync, n)
    if not emails:
        return "Ingen emails fundet."
    parts = []
    for i, e in enumerate(emails, 1):
        if "error" in e:
            return f"Email fejl: {e['error']}"
        parts.append(
            f"**{i}. Fra:** {e['from']}\n"
            f"**Emne:** {e['subject']}\n"
            f"**Dato:** {e['date']}\n"
            f"```\n{e['body'][:400]}\n```"
        )
    return "\n\n---\n\n".join(parts)


async def send_email(to: str, subject: str, body: str) -> str:
    if not _configured():
        return "Email ikke konfigureret."
    return await asyncio.get_event_loop().run_in_executor(
        None, _send_email_sync, to, subject, body
    )
