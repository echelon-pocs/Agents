import os
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

from models import Property

logger = logging.getLogger(__name__)

RECIPIENT = "Sofia.nuns@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _format_price(price) -> str:
    if price is None:
        return "Preço não indicado"
    return f"{price:,.0f} €".replace(",", ".")


def _format_area(area) -> str:
    if area is None:
        return "—"
    return f"{area:.0f} m²"


def _garage_text(prop: Property) -> str:
    if not prop.has_garage:
        return "Não mencionada"
    if prop.garage_spaces >= 2:
        return f"{prop.garage_spaces} lugares"
    return "Sim (lugares não especificados)"


def _amenities_stars(score: int) -> str:
    stars = "★" * score + "☆" * (5 - score)
    return stars


def _property_card(prop: Property) -> str:
    img_html = ""
    if prop.images:
        img_html = f'<img src="{prop.images[0]}" style="width:100%;max-width:480px;border-radius:6px;margin-bottom:12px;" alt="Foto do imóvel">'

    balcony = f"{prop.balcony_area_m2:.0f} m²" if prop.balcony_area_m2 else "Não especificada"
    outdoor = "✔ Sim" if prop.has_outdoor else "Não mencionado"
    rooms_text = f"T{prop.rooms}" if prop.rooms else "—"
    amenities_html = ""
    if prop.amenities_detail:
        amenities_html = f"""
        <tr>
          <td style="padding:4px 0;color:#555;width:160px;">Comodidades nearby</td>
          <td style="padding:4px 0;">{_amenities_stars(prop.amenities_score)} {prop.amenities_detail}</td>
        </tr>"""

    return f"""
    <div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:20px;margin-bottom:24px;font-family:Arial,sans-serif;">
      {img_html}
      <h2 style="margin:0 0 4px;font-size:18px;color:#1a1a1a;">
        <a href="{prop.url}" style="color:#1565c0;text-decoration:none;">{prop.title}</a>
      </h2>
      <p style="margin:0 0 12px;color:#666;font-size:14px;">📍 {prop.location} &nbsp;|&nbsp; Fonte: {prop.source}</p>
      <p style="margin:0 0 16px;font-size:24px;font-weight:bold;color:#2e7d32;">{_format_price(prop.price)}</p>
      <table style="border-collapse:collapse;width:100%;font-size:14px;">
        <tr>
          <td style="padding:4px 0;color:#555;width:160px;">Tipologia</td>
          <td style="padding:4px 0;font-weight:bold;">{rooms_text}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;color:#555;">Área total</td>
          <td style="padding:4px 0;">{_format_area(prop.area_m2)}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;color:#555;">Varanda/Terraço</td>
          <td style="padding:4px 0;">{balcony}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;color:#555;">Espaço exterior</td>
          <td style="padding:4px 0;">{outdoor}</td>
        </tr>
        <tr>
          <td style="padding:4px 0;color:#555;">Garagem</td>
          <td style="padding:4px 0;">{_garage_text(prop)}</td>
        </tr>
        {amenities_html}
      </table>
      {"<p style='margin:16px 0 0;font-size:13px;color:#777;'>" + prop.description[:300] + ("…" if len(prop.description) > 300 else "") + "</p>" if prop.description else ""}
      <p style="margin:16px 0 0;">
        <a href="{prop.url}" style="background:#1565c0;color:#fff;padding:10px 20px;border-radius:5px;text-decoration:none;font-size:14px;">Ver anúncio →</a>
      </p>
    </div>"""


def _scraper_health_html(scraper_health: dict) -> str:
    if not scraper_health:
        return ""
    rows = ""
    for name, h in scraper_health.items():
        zeros = h.get("consecutive_zeros", 0)
        mode = h.get("last_mode", "normal")
        if zeros == 0:
            badge = '<span style="color:#2e7d32;font-weight:bold;">OK</span>'
        elif zeros >= 3:
            badge = f'<span style="color:#c62828;font-weight:bold;">⚠ {zeros} runs sem resultados — modo {mode}</span>'
        else:
            badge = f'<span style="color:#e65100;">⚠ {zeros} run(s) sem resultados</span>'
        rows += f"<tr><td style='padding:3px 8px;color:#555;'>{name}</td><td style='padding:3px 8px;'>{badge}</td></tr>"
    return f"""
    <div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:16px;margin-bottom:16px;">
      <p style="margin:0 0 10px;font-size:13px;font-weight:bold;color:#333;">Estado dos scrapers</p>
      <table style="font-size:12px;border-collapse:collapse;width:100%">{rows}</table>
    </div>"""


def build_html_email(properties: List[Property], total_known: int, scraper_health: dict = None) -> str:
    today = datetime.now().strftime("%d de %B de %Y")
    cards = "".join(_property_card(p) for p in properties)
    no_results_note = ""
    if not properties:
        no_results_note = """
        <div style="background:#fff3e0;border:1px solid #ffe0b2;border-radius:10px;padding:16px;margin-bottom:16px;text-align:center;color:#e65100;">
          Nenhum imóvel novo encontrado hoje. Consulte o estado dos scrapers abaixo.
        </div>"""
    health_html = _scraper_health_html(scraper_health or {})
    return f"""
    <!DOCTYPE html>
    <html lang="pt">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body style="background:#f5f5f5;padding:20px;font-family:Arial,sans-serif;">
      <div style="max-width:640px;margin:0 auto;">
        <div style="background:#1565c0;color:#fff;padding:24px;border-radius:10px 10px 0 0;">
          <h1 style="margin:0;font-size:22px;">🏡 Novos Imóveis em Ermesinde</h1>
          <p style="margin:6px 0 0;opacity:0.85;">{today} — {len(properties)} novo(s) anúncio(s) encontrado(s)</p>
        </div>
        <div style="background:#e8f0fe;padding:12px 24px;font-size:13px;color:#333;">
          <strong>Critérios de pesquisa:</strong> T3+, varanda ≥20 m², cozinha+sala ≥35 m²,
          exterior, garagem, ≤380 000 €, Ermesinde e arredores (10 km) &nbsp;|&nbsp;
          Total acumulado na base de dados: {total_known} imóveis
        </div>
        <div style="padding:20px 0;">
          {no_results_note}
          {cards}
          {health_html}
        </div>
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:10px;padding:16px;font-size:12px;color:#999;text-align:center;">
          Esta pesquisa é automática e cobre: Idealista · Imovirtual · Casa.sapo · Supercasa · OLX · CustoJusto · ERA · RE/MAX<br>
          Para deixar de receber estes e-mails, contacte o administrador do sistema.
        </div>
      </div>
    </body>
    </html>"""


def send_email(properties: List[Property], total_known: int, scraper_health: dict = None) -> bool:
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    if not sender or not password:
        logger.error("EMAIL_SENDER and EMAIL_PASSWORD env vars must be set")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏡 {len(properties)} novo(s) imóvel(is) em Ermesinde — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"] = sender
    msg["To"] = RECIPIENT

    text_body = f"Foram encontrados {len(properties)} novo(s) imóvel(is).\n\n"
    for p in properties:
        text_body += f"• {p.title} — {_format_price(p.price)}\n  {p.url}\n\n"

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(build_html_email(properties, total_known, scraper_health=scraper_health), "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, RECIPIENT, msg.as_string())
        logger.info(f"Email sent to {RECIPIENT} with {len(properties)} properties")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False
