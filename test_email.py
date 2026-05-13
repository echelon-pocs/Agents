#!/usr/bin/env python3
"""Quick SMTP connection test — run this to diagnose email failures."""
import smtplib, ssl
from pathlib import Path

def load_env():
    config = {}
    p = Path(__file__).parent / ".env"
    if p.exists():
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                config[k.strip()] = v.strip()
    return config

cfg = load_env()
host = cfg.get("SMTP_HOST", "smtp.gmail.com")
port = int(cfg.get("SMTP_PORT", 587))
user = cfg.get("SMTP_USER", "")
pw   = cfg.get("SMTP_PASS", "")
to   = cfg.get("ALERT_EMAIL", user)

print(f"SMTP_HOST : {host}")
print(f"SMTP_PORT : {port}")
print(f"SMTP_USER : {user}")
print(f"SMTP_PASS : {'*' * len(pw)} ({len(pw)} chars)")
print(f"ALERT_EMAIL: {to}")
print()

try:
    print(f"Connecting to {host}:{port}...")
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=15) as s:
        s.ehlo()
        print("EHLO OK")
        s.starttls(context=ctx)
        print("STARTTLS OK")
        s.login(user, pw)
        print("LOGIN OK")
        s.sendmail(user, to, f"Subject: Test\r\n\r\nCrypto agent email test OK")
        print(f"TEST EMAIL SENT to {to}")
except Exception as e:
    print(f"ERROR: {e}")
