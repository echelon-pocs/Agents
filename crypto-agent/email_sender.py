"""
Email sender for daily crypto report and ENTER alerts.
Reads SMTP credentials from .env file.
Sends multipart/alternative (HTML + plain text) with full-report attachment.
"""

import re
import smtplib
# Matches position/setup card titles:
#   "BTC LONG"  "SUI SHORT"  (bare)
#   "⚠️ BTC LONG"  "🚨 ETH SHORT"  (danger-flagged position)
_CARD_TITLE_RE = re.compile(r'^[A-Z][A-Z0-9]{1,7}\s+(LONG|SHORT)\b')
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from html import escape
from pathlib import Path
from typing import Optional


# ── HTML palette ─────────────────────────────────────────────────────────────

_SECTION_COLORS = {
    # Crypto agent sections
    "MACRO REGIME":          "#1a1a2e",
    "YEN CARRY":             "#1a1a2e",
    "CYCLE VIEW":            "#2d1b69",
    "LIQUIDITY ANALYSIS":    "#0d3b5e",
    "OPEN POSITIONS":        "#1e3a2f",
    "SHORT-TERM SETUPS":     "#3d1c02",
    "LONG-TERM SETUPS":      "#2d1b69",
    "WAITING":               "#2d2d2d",
    "CHANGES TODAY":         "#1a3a1a",
    # Portfolio agent sections
    "MACRO COMMENTARY":      "#1a1a2e",
    "WTI":                   "#3d2000",
    "BRENT":                 "#3d2800",
    "SPX":                   "#003d2d",
    "EQUITIES":              "#003d2d",
    "VWCE / VWRL":           "#003d2d",
    "GOLD":                  "#3d3000",
    "SILVER":                "#2d3030",
    "SETUPS":                "#3d1c02",
}

# keyword → (emoji, text-color, background-color)
_BADGES = {
    "BEARISH":          ("🔴", "#9b1c1c", "#fde8e8"),
    "BULLISH":          ("🟢", "#14532d", "#dcfce7"),
    "NEUTRAL":          ("⚪", "#4b5563", "#f3f4f6"),
    "BIFURCATED":       ("🟡", "#92400e", "#fef3c7"),
    "CARRY_STABLE":     ("✅", "#14532d", "#dcfce7"),
    "CARRY_STRESS":     ("⚠️", "#92400e", "#fef3c7"),
    "CARRY_UNWIND":     ("🔶", "#9b1c1c", "#fde8e8"),
    "CARRY_COLLAPSE":   ("🚨", "#7f1d1d", "#fecaca"),
    "NORMAL":           ("✅", "#14532d", "#dcfce7"),
    "FLAT":             ("➖", "#92400e", "#fef3c7"),
    "INVERTED":         ("🔻", "#9b1c1c", "#fde8e8"),
    "ELEVATED":         ("⚠️", "#92400e", "#fef3c7"),
    "HIGH":             ("🚨", "#9b1c1c", "#fde8e8"),
    "CRITICAL":         ("🚨", "#7f1d1d", "#fecaca"),
    "EXTREME_LONGS":    ("🚨", "#9b1c1c", "#fde8e8"),
    "EXTREME_SHORTS":   ("🟡", "#92400e", "#fef3c7"),
    "ENTER":            ("🔴", "#9b1c1c", "#fde8e8"),
    "APPROACHING":      ("🟡", "#92400e", "#fef3c7"),
    "WAITING":          ("⚪", "#4b5563", "#f3f4f6"),
    "INVALIDATED":      ("❌", "#6b7280", "#f9fafb"),
    "BEAR":             ("🔴", "#9b1c1c", "#fde8e8"),
    "BOTTOM":           ("🟡", "#92400e", "#fef3c7"),
    "BULL":             ("🟢", "#14532d", "#dcfce7"),
    "ACCUMULATION":     ("🟢", "#14532d", "#dcfce7"),
    "DISTRIBUTION":     ("🔴", "#9b1c1c", "#fde8e8"),
    "PRE_HALVING":      ("🟣", "#4c1d95", "#ede9fe"),
    "SHORT_TERM":       ("⚡", "#1e40af", "#dbeafe"),
    "MEDIUM_TERM":      ("📅", "#1e40af", "#dbeafe"),
    "LONG_TERM":        ("📆", "#4c1d95", "#ede9fe"),
    "Aligned":          ("✅", "#14532d", "#dcfce7"),
    "CONFLICT":         ("⚠️", "#9b1c1c", "#fde8e8"),
}

_HTML_CSS = """
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       font-size:14px;color:#111827;background:#f3f4f6;margin:0;padding:12px}
  .wrap{max-width:560px;margin:0 auto}
  .hdr{background:#0f0f1f;color:#fff;padding:14px 16px;border-radius:10px 10px 0 0}
  .hdr-label{font-size:10px;letter-spacing:2px;text-transform:uppercase;
              color:#9ca3af;margin-bottom:6px}
  .hdr-title{font-size:20px;font-weight:700;margin-bottom:2px}
  .hdr-sub{font-size:12px;color:#d1d5db;line-height:1.6}
  .pill-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .pill{background:rgba(255,255,255,.12);border-radius:20px;
        padding:3px 10px;font-size:11px;font-weight:600;color:#e5e7eb}
  .section{background:#fff;border:1px solid #e5e7eb;
           border-radius:8px;margin-top:10px;overflow:hidden}
  .sec-head{padding:8px 14px;font-size:10px;font-weight:700;
            letter-spacing:1.5px;text-transform:uppercase;color:#fff}
  .sec-body{padding:10px 14px}
  .kv{display:flex;align-items:baseline;gap:6px;
      padding:4px 0;border-bottom:1px solid #f3f4f6;font-size:13px}
  .kv:last-child{border-bottom:none}
  .kv-key{color:#6b7280;min-width:72px;flex-shrink:0;font-size:12px}
  .kv-val{font-family:'Courier New',monospace;flex:1}
  .bullet{padding:5px 0 5px 14px;border-left:3px solid #d1d5db;
          margin:4px 0;font-size:13px;line-height:1.55}
  .bullet.info{border-left-color:#3b82f6}
  .warn-box{background:#fef3c7;border-left:3px solid #f59e0b;
            padding:7px 10px;margin:5px 0;border-radius:0 4px 4px 0;font-size:13px}
  .danger-box{background:#fde8e8;border-left:3px solid #ef4444;
              padding:7px 10px;margin:5px 0;border-radius:0 4px 4px 0;font-size:13px}
  .card{background:#f9fafb;border:1px solid #e5e7eb;
        border-radius:6px;padding:9px 12px;margin:6px 0}
  .card-title{font-weight:700;font-size:13px;margin-bottom:6px;color:#111827}
  .badge{display:inline-flex;align-items:center;gap:3px;padding:1px 7px;
         border-radius:10px;font-size:11px;font-weight:700;
         white-space:nowrap;vertical-align:middle}
  .mono{font-family:'Courier New',monospace}
  .pos{color:#15803d;font-weight:600}
  .neg{color:#dc2626;font-weight:600}
  .dim{color:#9ca3af;font-size:11px}
  .change-item{padding:3px 0;font-size:13px;color:#374151}
  hr{border:none;border-top:1px solid #e5e7eb;margin:8px 0}
</style>
"""

_KNOWN_SECTIONS = [
    # Crypto agent
    "MACRO REGIME", "YEN CARRY", "CYCLE VIEW", "LIQUIDITY ANALYSIS",
    "OPEN POSITIONS", "SHORT-TERM SETUPS", "LONG-TERM SETUPS",
    "WAITING", "CHANGES TODAY",
    # Portfolio agent
    "MACRO COMMENTARY", "WTI", "BRENT", "SPX", "EQUITIES",
    "VWCE / VWRL", "GOLD", "SILVER", "SETUPS",
]


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _badge(word: str) -> str:
    b = _BADGES.get(word)
    if not b:
        return escape(word)
    emoji, fg, bg = b
    return (f'<span class="badge" style="color:{fg};background:{bg}">'
            f'{emoji}&nbsp;{escape(word)}</span>')


def _colorize(text: str) -> str:
    """Inline-color known keywords, prices, and P&L percentages."""
    safe = escape(text)

    # Status badges — whole-word only
    for kw in sorted(_BADGES, key=len, reverse=True):
        safe = re.sub(
            r'(?<![A-Za-z_])' + re.escape(kw) + r'(?![A-Za-z_])',
            _badge(kw),
            safe,
        )

    # Bold prices: $103,000 or $103.5k
    safe = re.sub(
        r'\$(\d[\d,\.kKmMbB]*)',
        r'<strong class="mono">$\1</strong>',
        safe,
    )

    # Colour P&L percentages
    def _pct(m):
        try:
            val = float(m.group(0).rstrip('%').replace(',', ''))
        except ValueError:
            return m.group(0)
        cls = "pos" if val > 0 else "neg" if val < 0 else ""
        return f'<span class="{cls}">{escape(m.group(0))}</span>' if cls else m.group(0)

    safe = re.sub(r'[+\-]?\d+\.?\d*%', _pct, safe)
    return safe


def _is_divider(s: str) -> bool:
    return bool(s) and all(c in '-═─' for c in s)


def _section_header(s: str) -> Optional[str]:
    for sec in _KNOWN_SECTIONS:
        if s == sec:
            return sec
        # Allow trailing tier annotations the model sometimes appends:
        # "WTI  [TIER 1 — DEEP ANALYSIS]" → "WTI"
        # "SILVER (8PSB)" → "SILVER"
        # Exclude kv lines: "SPX   : 5800" must NOT match "SPX" section.
        if (s.startswith(sec)
                and len(s) > len(sec)
                and s[len(sec)] in ' \t(['
                and ':' not in s):
            return sec
    return None


# ── Section renderers ─────────────────────────────────────────────────────────

def _open_section(title: str) -> str:
    color = _SECTION_COLORS.get(title, "#1f2937")
    return (f'<div class="section">'
            f'<div class="sec-head" style="background:{color}">{escape(title)}</div>'
            f'<div class="sec-body">')


def _close_section() -> str:
    return '</div></div>'


def _render_kv_line(line: str) -> str:
    """Render a key: value line as a styled row."""
    key, _, val = line.partition(':')
    val = val.strip()
    return (f'<div class="kv">'
            f'<span class="kv-key">{escape(key.strip())}</span>'
            f'<span class="kv-val">{_colorize(val)}</span>'
            f'</div>')


def _render_bullet(line: str) -> str:
    content = line.lstrip('•· ').strip()
    return f'<div class="bullet info">{_colorize(content)}</div>'


def _render_card_line(line: str) -> str:
    """Inside a position/setup card, render indented key: value."""
    stripped = line.strip()
    if ':' in stripped:
        return _render_kv_line(stripped)
    return f'<div style="font-size:13px;padding:2px 0">{_colorize(stripped)}</div>'


# ── Main renderer ─────────────────────────────────────────────────────────────

def render_html_email(plain_body: str) -> str:
    """Convert the plain-text [EMAIL] body to a styled HTML email."""
    lines = plain_body.splitlines()
    out = ['<!DOCTYPE html><html lang="en"><head>'
           '<meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           _HTML_CSS,
           '</head><body><div class="wrap">']

    in_section  = False
    in_card     = False    # inside a position/setup card block
    in_bullet   = False    # last rendered line was a bullet; accumulate continuations
    header_done = False

    def close_bullet():
        nonlocal in_bullet
        if in_bullet:
            out.append('</div>')   # close open .bullet div
            in_bullet = False

    def close_card():
        nonlocal in_card
        close_bullet()
        if in_card:
            out.append('</div>')  # close .card
            in_card = False

    def close_section():
        nonlocal in_section
        close_bullet()
        close_card()
        if in_section:
            out.append(_close_section())
            in_section = False

    i = 0
    while i < len(lines):
        raw  = lines[i]
        line = raw.strip()
        i   += 1

        # ── Skip empty lines in cards/bullets; light spacer elsewhere ──
        if not line:
            if in_card or in_bullet:
                pass
            else:
                out.append('<div style="height:4px"></div>')
            continue

        # ── Dividers ──
        if _is_divider(line):
            close_bullet()
            if in_card:
                close_card()
            continue

        # ── ⚠️ / 🚨 lines: position card if "SYM LONG/SHORT" follows,
        #    otherwise a plain alert box ──
        if line.startswith('⚠️') or line.startswith('🚨'):
            close_bullet()
            rest = line[2:].strip()   # strip emoji + space
            if _CARD_TITLE_RE.match(rest):
                # danger-flagged position card — same card style as plain positions,
                # emoji in the title provides the visual cue
                close_card()
                out.append(f'<div class="card">'
                            f'<div class="card-title">{_colorize(line)}</div>')
                in_card = True
            else:
                close_card()
                cls = 'warn-box' if line.startswith('⚠️') else 'danger-box'
                out.append(f'<div class="{cls}">{_colorize(line)}</div>')
            continue

        # ── Section header ──
        sec = _section_header(line)
        if sec:
            close_section()
            out.append(_open_section(sec))
            in_section = True
            continue

        # ── Email header block ──
        if not header_done and ("CRYPTO DAILY BRIEF" in line or "PORTFOLIO BRIEF" in line):
            is_portfolio = "PORTFOLIO BRIEF" in line
            label = "Portfolio Intelligence" if is_portfolio else "Crypto Market Intelligence"
            title = "Portfolio Brief" if is_portfolio else "Daily Brief"
            out.append(f'<div class="hdr">'
                       f'<div class="hdr-label">{label}</div>'
                       f'<div class="hdr-title">{title}</div>')
            # consume following header lines until first divider or section
            sub_lines = []
            while i < len(lines):
                nxt = lines[i].strip()
                if _is_divider(nxt) or _section_header(nxt):
                    break
                if nxt:
                    sub_lines.append(nxt)
                i += 1
            if sub_lines:
                out.append(f'<div class="hdr-sub">{escape(sub_lines[0])}</div>')
            if len(sub_lines) > 1:
                # BTC/Dom/F&G line — render as pills
                out.append('<div class="pill-row">')
                for part in sub_lines[1].split('|'):
                    out.append(f'<span class="pill">{escape(part.strip())}</span>')
                out.append('</div>')
            out.append('</div>')  # close .hdr
            header_done = True
            continue

        # ── Bullet continuation line (indented, follows a bullet, not in a card) ──
        if in_bullet and not in_card and (raw.startswith('  ') or raw.startswith('\t')):
            out.append(f' {_colorize(line)}')
            continue

        # ── Bullet points ──
        if line.startswith(('•', '·')):
            close_bullet()
            close_card()
            content = line.lstrip('•· ').strip()
            out.append(f'<div class="bullet info">{_colorize(content)}')
            in_bullet = True
            continue

        # ── Change-log items (lines starting with •, NEW, ENTER, ADOPTED, etc.) ──
        if in_section and line.startswith(('NEW', 'ENTER', 'ADOPTED', 'REVISED',
                                           'INVALIDATED', 'COMPLETED', 'HOLD')):
            close_bullet()
            close_card()
            out.append(f'<div class="change-item">▸ {_colorize(line)}</div>')
            continue

        # ── Setup / position card titles (emoji-prefixed or "SYM LONG/SHORT" pattern) ──
        if in_section and (line[0] in ('🔴', '🟣', '🟡', '🟠', '⚪', '🟢') or
                           bool(_CARD_TITLE_RE.match(line))):
            close_card()
            out.append(f'<div class="card"><div class="card-title">{_colorize(line)}</div>')
            in_card = True
            continue

        # ── Indented key:value inside a card ──
        if in_card and (raw.startswith('  ') or raw.startswith('\t')):
            out.append(_render_card_line(line))
            continue

        # ── Key:value line at section level (macro cards, cycle, etc.) ──
        if in_section and ':' in line and not line.startswith('http'):
            close_bullet()
            out.append(_render_kv_line(line))
            continue

        # ── SHORT bias / LONG bias lines ──
        if in_section and (line.startswith('SHORT bias') or line.startswith('LONG  bias') or
                           line.startswith('LONG bias')):
            close_bullet()
            out.append(_render_kv_line(line))
            continue

        # ── Plain text line (cycle thesis, narrative) ──
        close_bullet()
        out.append(f'<div style="font-size:13px;padding:3px 0;line-height:1.55;'
                   f'color:#374151">{_colorize(line)}</div>')

    close_section()

    # Footer
    out.append('<div style="text-align:center;padding:16px 0 8px;'
               'font-size:11px;color:#9ca3af">'
               'Full analysis attached · Crypto Market Intelligence Agent</div>')
    out.append('</div></body></html>')
    return '\n'.join(out)


# ── SMTP helpers ──────────────────────────────────────────────────────────────

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


def send_report(subject: str, body: str, is_alert: bool = False,
                attachment: str = "", attachment_filename: str = "") -> bool:
    """
    Send the daily report as a multipart/alternative email (HTML + plain text)
    with an optional plain-text attachment containing the full response.
    Returns True on success.
    """
    cfg = load_smtp_config()

    smtp_host = cfg.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(cfg.get("SMTP_PORT", 587))
    smtp_user = cfg.get("SMTP_USER", "")
    smtp_pass = cfg.get("SMTP_PASS", "")

    primary    = cfg.get("ALERT_EMAIL", smtp_user)
    recipients = [primary]

    if not smtp_user or not smtp_pass:
        print("[Email] ERROR: SMTP_USER or SMTP_PASS not found in .env")
        return False

    # Outer container for alternative body + attachment
    outer = MIMEMultipart("mixed")
    outer["Subject"] = subject
    outer["From"]    = smtp_user
    outer["To"]      = ", ".join(recipients)

    # Inner alternative: plain text fallback + HTML preferred
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body, "plain", "utf-8"))
    try:
        html_body = render_html_email(body)
        alt.attach(MIMEText(html_body, "html", "utf-8"))
    except Exception as e:
        print(f"[Email] HTML render failed ({e}) — sending plain text only")

    outer.attach(alt)

    if attachment and attachment_filename:
        part = MIMEBase("text", "plain")
        part.set_payload(attachment.encode("utf-8"))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=attachment_filename)
        outer.attach(part)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, outer.as_string())
        att_note = f" + {attachment_filename}" if attachment_filename else ""
        print(f"[Email] Sent to {', '.join(recipients)}: {subject}{att_note}")
        return True
    except Exception as e:
        print(f"[Email] ERROR sending email: {e}")
        return False


def build_subject(macro_bias: str, setup_count: int,
                  enter_count: int, date_str: str) -> str:
    if enter_count > 0:
        return f"🔴 ENTRY ALERT — {date_str} | {macro_bias} | {enter_count} ENTER"
    return f"📊 Crypto Brief — {date_str} | {macro_bias} | {setup_count} setups"
