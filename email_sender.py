"""
Email sender for daily crypto report and ENTER alerts.
Reads SMTP credentials from .env file.
"""

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from typing import Optional


def load_smtp_config() -> dict:
    env_path = Path(__file__).parent / ".env"
    config = {}
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    return config


def send_report(subject: str, body: str, is_alert: bool = False) -> bool:
    """
    Send the daily report or ENTER alert email.
    Returns True on success, False on failure.
    """
    cfg = load_smtp_config()

    smtp_host = cfg.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(cfg.get("SMTP_PORT", 587))
    smtp_user = cfg.get("SMTP_USER", "")
    smtp_pass = cfg.get("SMTP_PASS", "")
    to_addr   = cfg.get("ALERT_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        print("[Email] ERROR: SMTP_USER or SMTP_PASS not found in .env")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr

    # Plain text body
    part = MIMEText(body, "plain", "utf-8")
    msg.attach(part)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_addr, msg.as_string())
        print(f"[Email] Sent to {to_addr}: {subject}")
        return True
    except Exception as e:
        print(f"[Email] ERROR sending email: {e}")
        return False


def build_subject(macro_bias: str, setup_count: int,
                  enter_count: int, date_str: str) -> str:
    if enter_count > 0:
        return f"🔴 ENTRY ALERT + Daily Report — {date_str} | {macro_bias} | {enter_count} ENTER"
    return f"📊 Crypto Daily Report — {date_str} | {macro_bias} | {setup_count} Active Setups"
