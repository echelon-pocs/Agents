import html
import os
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

from models import Property
from scoring import score_label, score_color

logger = logging.getLogger(__name__)

RECIPIENTS = ["Sofia.nuns@gmail.com", "pedromiguelralves@gmail.com"]


# ── formatting helpers ────────────────────────────────────────────────────────

def _fmt_price(price) -> str:
    if price is None:
        return "Preço não indicado"
    return f"{price:,.0f} €".replace(",", ".")


def _fmt_price_prop(prop: Property) -> str:
    if prop.price is not None:
        return _fmt_price(prop.price)
    pf = prop.raw_data.get("price_from")
    pt = prop.raw_data.get("price_to")
    if pf and pt:
        return f"A partir de {_fmt_price(pf)} até {_fmt_price(pt)}"
    if pf:
        return f"A partir de {_fmt_price(pf)}"
    return "Preço não indicado"


def _fmt_area(area) -> str:
    return f"{area:.0f} m²" if area else "—"


def _garage_text(prop: Property) -> str:
    if not prop.has_garage:
        return "Não mencionada"
    if prop.garage_spaces >= 2:
        return f"{prop.garage_spaces} lugares ✔"
    return "Sim (lugares n/d)"


def _stars(score: int, max_score: int = 5) -> str:
    filled = min(score, max_score)
    return "★" * filled + "☆" * (max_score - filled)


# ── property card ─────────────────────────────────────────────────────────────

def _property_card(prop: Property, is_price_drop: bool = False) -> str:
    img_html = ""
    if prop.images:
        img_html = (
            f'<img src="{prop.images[0]}" '
            f'style="width:100%;max-width:480px;border-radius:6px;margin-bottom:12px;" '
            f'alt="Foto do imóvel">'
        )

    # Score badge
    sc = prop.match_score
    sc_label = score_label(sc)
    sc_color = score_color(sc)
    score_badge = (
        f'<span style="background:{sc_color};color:#fff;font-size:11px;'
        f'padding:2px 8px;border-radius:12px;font-weight:bold;margin-left:8px;">'
        f'Score {sc} — {sc_label}</span>'
    )

    # Price-drop banner
    drop_banner = ""
    if is_price_drop and prop.price_dropped_from:
        diff = prop.price_dropped_from - prop.price
        drop_banner = (
            f'<div style="background:#e8f5e9;border-left:4px solid #2e7d32;'
            f'padding:8px 12px;margin-bottom:12px;border-radius:0 6px 6px 0;font-size:13px;">'
            f'↓ Baixou de <s>{_fmt_price(prop.price_dropped_from)}</s> para '
            f'<strong>{_fmt_price(prop.price)}</strong> '
            f'<span style="color:#2e7d32;font-weight:bold;">(-{_fmt_price(diff)})</span>'
            f'</div>'
        )

    # Distance badge
    dist_html = ""
    if prop.distance_km is not None:
        dist_html = f' &nbsp;|&nbsp; {prop.distance_km:.1f} km de Ermesinde'

    # Balcony
    balcony_ok = prop.balcony_area_m2 and prop.balcony_area_m2 >= 20
    balcony_text = (
        f'{prop.balcony_area_m2:.0f} m² {"✔" if balcony_ok else "⚠ (< 20 m²)"}'
        if prop.balcony_area_m2 else "Não especificada"
    )

    # Kitchen + living (from detail scraping)
    kitchen_living = prop.raw_data.get("kitchen_living_combined_m2")
    kl_ok = kitchen_living and kitchen_living >= 35
    kl_row = ""
    if kitchen_living:
        kl_row = (
            f'<tr><td style="padding:4px 0;color:#555;width:160px;">Cozinha+Sala</td>'
            f'<td style="padding:4px 0;">{kitchen_living:.0f} m² {"✔" if kl_ok else "⚠ (< 35 m²)"}</td></tr>'
        )

    amenities_row = ""
    if prop.amenities_detail:
        amenities_row = (
            f'<tr><td style="padding:4px 0;color:#555;vertical-align:top;">Comodidades</td>'
            f'<td style="padding:4px 0;">{_stars(prop.amenities_score)} {prop.amenities_detail}'
            f'<br><span style="font-size:11px;color:#aaa;">nº de locais num raio de 800 m</span></td></tr>'
        )

    outdoor = "✔ Sim" if prop.has_outdoor else "Não mencionado"
    rooms_text = f"T{prop.rooms}" if prop.rooms else "—"
    card_border = "2px solid #2e7d32" if is_price_drop else "1px solid #e0e0e0"

    return f"""
    <div style="background:#fff;border:{card_border};border-radius:10px;padding:20px;margin-bottom:24px;font-family:Arial,sans-serif;">
      {drop_banner}
      {img_html}
      <h2 style="margin:0 0 4px;font-size:17px;color:#1a1a1a;">
        <a href="{html.escape(prop.url)}" style="color:#1565c0;text-decoration:none;">{prop.title}</a>
        {score_badge}
      </h2>
      <p style="margin:0 0 12px;color:#666;font-size:13px;">📍 {prop.location}{dist_html} &nbsp;|&nbsp; {prop.source}</p>
      <p style="margin:0 0 16px;font-size:22px;font-weight:bold;color:#2e7d32;">{_fmt_price_prop(prop)}</p>
      <table style="border-collapse:collapse;width:100%;font-size:14px;">
        <tr>
          <td style="padding:4px 0;color:#555;width:160px;">Tipologia</td>
          <td style="padding:4px 0;font-weight:bold;">{rooms_text}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;color:#555;">Área total</td>
          <td style="padding:4px 0;">{_fmt_area(prop.area_m2)}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;color:#555;">Varanda/Terraço</td>
          <td style="padding:4px 0;">{balcony_text}</td>
        </tr>
        {kl_row}
        <tr>
          <td style="padding:4px 0;color:#555;">Espaço exterior</td>
          <td style="padding:4px 0;">{outdoor}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;color:#555;">Garagem</td>
          <td style="padding:4px 0;">{_garage_text(prop)}</td>
        </tr>
        {amenities_row}
      </table>
      {"<p style='margin:16px 0 0;font-size:13px;color:#777;'>" + prop.description[:300] + ("…" if len(prop.description) > 300 else "") + "</p>" if prop.description else ""}
      <p style="margin:16px 0 0;">
        <a href="{html.escape(prop.url)}" style="background:#1565c0;color:#fff;padding:10px 20px;border-radius:5px;text-decoration:none;font-size:14px;">Ver anúncio →</a>
      </p>
    </div>"""


# ── weekly digest ─────────────────────────────────────────────────────────────

def _digest_card(row: dict) -> str:
    import json
    images = row.get("images") or []
    if isinstance(images, str):
        images = json.loads(images)
    img_html = (
        f'<img src="{images[0]}" style="width:80px;height:60px;object-fit:cover;'
        f'border-radius:4px;margin-right:12px;float:left;" alt="">'
        if images else ""
    )
    price_str = _fmt_price(row.get("price"))
    rooms_str = f"T{row['rooms']}" if row.get("rooms") else "T?"
    sc = row.get("match_score", 0)
    sc_color = score_color(sc)
    dist = row.get("distance_km")
    dist_str = f" · {dist:.1f} km" if dist else ""
    return (
        f'<div style="padding:10px;border-bottom:1px solid #eee;overflow:hidden;">'
        f'{img_html}'
        f'<a href="{html.escape(row["url"])}" style="color:#1565c0;text-decoration:none;font-weight:bold;font-size:14px;">{row["title"][:70]}</a><br>'
        f'<span style="font-size:13px;color:#2e7d32;font-weight:bold;">{price_str}</span>'
        f' &nbsp; <span style="font-size:12px;color:#555;">{rooms_str} · {row.get("location","")}{dist_str}</span>'
        f' &nbsp; <span style="background:{sc_color};color:#fff;font-size:11px;padding:1px 6px;border-radius:10px;">Score {sc}</span>'
        f'</div>'
    )


def _weekly_digest_html(digest_rows: list) -> str:
    if not digest_rows:
        return ""
    cards = "".join(_digest_card(r) for r in digest_rows)
    return f"""
    <div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:0;margin-bottom:24px;overflow:hidden;">
      <div style="background:#37474f;color:#fff;padding:12px 16px;">
        <strong>📋 Resumo semanal — Top imóveis em base de dados</strong>
      </div>
      {cards}
    </div>"""


# ── scraper health ────────────────────────────────────────────────────────────

def _health_html(scraper_health: dict) -> str:
    if not scraper_health:
        return ""
    rows = ""
    for name, h in scraper_health.items():
        zeros = h.get("consecutive_zeros", 0)
        mode = h.get("last_mode", "normal")
        if zeros == 0:
            badge = '<span style="color:#2e7d32;font-weight:bold;">OK</span>'
        elif zeros >= 4:
            badge = f'<span style="color:#c62828;font-weight:bold;">⚠ {zeros} runs sem resultados — tier={mode}</span>'
        elif zeros >= 2:
            badge = f'<span style="color:#e65100;">⚠ {zeros} runs — tier={mode}</span>'
        else:
            badge = f'<span style="color:#f9a825;">1 run sem resultado</span>'
        rows += f"<tr><td style='padding:3px 8px;color:#555;font-size:12px;'>{name}</td><td style='padding:3px 8px;font-size:12px;'>{badge}</td></tr>"
    return f"""
    <div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:16px;margin-bottom:16px;">
      <p style="margin:0 0 10px;font-size:13px;font-weight:bold;color:#333;">Estado dos scrapers</p>
      <table style="border-collapse:collapse;width:100%;">{rows}</table>
    </div>"""


# ── email builder ─────────────────────────────────────────────────────────────

def build_html_email(
    new_properties: List[Property],
    price_drops: List[Property],
    total_known: int,
    scraper_health: dict = None,
    weekly_digest: list = None,
) -> str:
    today = datetime.now().strftime("%d de %B de %Y")
    total_shown = len(new_properties) + len(price_drops)

    new_cards = "".join(_property_card(p) for p in new_properties)

    drop_section = ""
    if price_drops:
        drop_cards = "".join(_property_card(p, is_price_drop=True) for p in price_drops)
        drop_section = f"""
        <div style="background:#e8f5e9;border-radius:10px;padding:16px;margin-bottom:20px;">
          <h2 style="margin:0 0 16px;font-size:16px;color:#1b5e20;">
            ↓ Descidas de preço ({len(price_drops)})
          </h2>
          {drop_cards}
        </div>"""

    no_results_note = ""
    if not new_properties and not price_drops:
        no_results_note = """
        <div style="background:#fff3e0;border:1px solid #ffe0b2;border-radius:10px;padding:16px;margin-bottom:16px;text-align:center;color:#e65100;">
          Nenhum imóvel novo ou atualizado hoje.
        </div>"""

    digest_html = _weekly_digest_html(weekly_digest or [])

    platforms = "Idealista · Imovirtual · OLX · Casa.sapo · Supercasa · ERA · RE/MAX · CustoJusto · Century21 · BPI · Predimed · LugarCerto"

    return f"""
    <!DOCTYPE html>
    <html lang="pt">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body style="background:#f5f5f5;padding:20px;font-family:Arial,sans-serif;">
      <div style="max-width:640px;margin:0 auto;">
        <div style="background:#1565c0;color:#fff;padding:24px;border-radius:10px 10px 0 0;">
          <h1 style="margin:0;font-size:22px;">🏡 Imóveis em Ermesinde</h1>
          <p style="margin:6px 0 0;opacity:0.85;">{today} — {total_shown} anúncio(s) para ver</p>
        </div>
        <div style="background:#e8f0fe;padding:12px 24px;font-size:13px;color:#333;">
          <strong>Critérios:</strong> T3+, varanda ou terraço ≥20 m², cozinha+sala ≥20 m²,
          garagem, ≤380 000 €, concelhos de Valongo, Gondomar e Maia &nbsp;|&nbsp;
          Base de dados: {total_known} imóveis
        </div>
        <div style="padding:20px 0;">
          {no_results_note}
          {drop_section}
          {new_cards}
          {digest_html}
        </div>
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:16px;font-size:12px;color:#999;text-align:center;">
          {platforms}<br>
          Para deixar de receber estes e-mails, contacte o administrador.
        </div>
      </div>
    </body>
    </html>"""


# ── sender ────────────────────────────────────────────────────────────────────

def send_email(
    new_properties: List[Property],
    price_drops: List[Property],
    total_known: int,
    scraper_health: dict = None,
    weekly_digest: list = None,
) -> bool:
    sender = os.environ.get("SMTP_USER") or os.environ.get("EMAIL_SENDER")
    password = os.environ.get("SMTP_PASS") or os.environ.get("EMAIL_PASSWORD")
    if not sender or not password:
        logger.error("SMTP_USER and SMTP_PASS env vars must be set")
        return False

    n_new = len(new_properties)
    n_drops = len(price_drops)
    parts = []
    if n_new:
        parts.append(f"{n_new} novo(s)")
    if n_drops:
        parts.append(f"{n_drops} descida(s) de preço")
    if not parts:
        parts = ["Relatório diário"]
    subject = f"🏡 Ermesinde: {', '.join(parts)} — {datetime.now().strftime('%d/%m/%Y')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(RECIPIENTS)

    text_body = f"{'='*50}\nImóveis em Ermesinde — {datetime.now().strftime('%d/%m/%Y')}\n{'='*50}\n\n"
    for label, group in [("NOVO", new_properties), ("DESCIDA DE PREÇO", price_drops)]:
        for p in group:
            drop = f" (era {_fmt_price(p.price_dropped_from)})" if p.price_dropped_from else ""
            text_body += f"[{label}] {p.title}\n  {_fmt_price_prop(p)}{drop} | {p.location} | Score {p.match_score}\n  {p.url}\n\n"

    html = build_html_email(
        new_properties=new_properties,
        price_drops=price_drops,
        total_known=total_known,
        scraper_health=scraper_health,
        weekly_digest=weekly_digest,
    )

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, RECIPIENTS, msg.as_string())
        logger.info(f"Email sent — {n_new} new, {n_drops} price drops")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False
