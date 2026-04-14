"""
US Trade Tracker — Alerta Diaria por Email
==========================================
Este script es ejecutado automáticamente cada día por GitHub Actions.
Obtiene las transacciones más recientes y envía un resumen por email.

Variables de entorno requeridas (configuradas como GitHub Secrets):
  - GMAIL_USER         → Tu dirección de Gmail (ej: tunombre@gmail.com)
  - GMAIL_APP_PASSWORD → Contraseña de aplicación de Gmail (NO tu contraseña normal)
  - ALERT_EMAIL        → Email donde quieres recibir las alertas
"""

import os
import sys
import time
import smtplib
import requests
import pandas as pd
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

HEADERS = {
    "User-Agent": "USTradeTracker/1.0 (github.com/usuario/trade-tracker; info@ejemplo.com)",
    "Accept-Encoding": "gzip, deflate",
}

YESTERDAY = datetime.now() - timedelta(days=1)
TODAY_STR = datetime.now().strftime("%d/%m/%Y")


# ─────────────────────────────────────────────
# OBTENCIÓN DE DATOS DEL CONGRESO
# ─────────────────────────────────────────────

def fetch_congressional_trades() -> pd.DataFrame:
    """
    Obtiene los trades del Congreso publicados ayer.
    Combina Cámara de Representantes y Senado.
    """
    dfs = []

    # ── Cámara de Representantes ──────────────
    try:
        url = "https://house-stock-watcher-data.s3-us-east-2.amazonaws.com/data/all_transactions.json"
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        df = pd.DataFrame(r.json())

        if "representative" in df.columns:
            df = df.rename(columns={"representative": "name"})
        elif "owner" in df.columns:
            df = df.rename(columns={"owner": "name"})
        if "type" in df.columns:
            df = df.rename(columns={"type": "trade_type"})

        df["chamber"] = "Cámara de Representantes"
        dfs.append(df)
        print(f"   ✓ House: {len(df)} registros totales cargados")
    except Exception as e:
        print(f"   ✗ Error cargando datos de la Cámara: {e}")

    # ── Senado ───────────────────────────────
    try:
        url = "https://senate-stock-watcher-data.s3-us-east-2.amazonaws.com/aggregate/all_transactions.json"
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        df = pd.DataFrame(r.json())

        if "senator" in df.columns:
            df = df.rename(columns={"senator": "name"})
        elif "owner" in df.columns:
            df = df.rename(columns={"owner": "name"})
        if "type" in df.columns:
            df = df.rename(columns={"type": "trade_type"})

        df["chamber"] = "Senado"
        dfs.append(df)
        print(f"   ✓ Senate: {len(df)} registros totales cargados")
    except Exception as e:
        print(f"   ✗ Error cargando datos del Senado: {e}")

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)

    # Filtrar por fecha de divulgación reciente (últimos 2 días)
    date_col = None
    for col in ["disclosure_date", "transaction_date"]:
        if col in combined.columns:
            date_col = col
            break

    if date_col:
        combined[date_col] = pd.to_datetime(combined[date_col], errors="coerce", dayfirst=False)
        cutoff = datetime.now() - timedelta(days=2)
        recent = combined[combined[date_col] >= cutoff].copy()
        print(f"   → {len(recent)} trades recientes del Congreso (últimas 48h)")
        return recent

    return combined.head(50)  # Fallback: devolver los primeros 50


# ─────────────────────────────────────────────
# OBTENCIÓN DE DATOS DE INSIDERS (SEC EDGAR)
# ─────────────────────────────────────────────

def parse_form4_xml(xml_bytes: bytes) -> list[dict]:
    """Parsea un archivo XML de Formulario 4 y extrae las transacciones."""
    trades = []
    try:
        root = ET.fromstring(xml_bytes)

        company = root.findtext(".//issuerName", "N/A").strip()
        ticker  = root.findtext(".//issuerTradingSymbol", "").strip().upper()
        owner   = root.findtext(".//rptOwnerName", "N/A").strip()
        is_off  = root.findtext(".//isOfficer", "0")
        title   = root.findtext(".//officerTitle", "").strip()
        is_dir  = root.findtext(".//isDirector", "0")
        role    = title if (is_off == "1" and title) else ("Director" if is_dir == "1" else "Accionista")

        for tx in root.findall(".//nonDerivativeTransaction"):
            date   = tx.findtext("transactionDate/value", "").strip()
            sh_s   = tx.findtext("transactionAmounts/transactionShares/value", "0")
            pr_s   = tx.findtext("transactionAmounts/transactionPricePerShare/value", "0")
            action = tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value", "").strip()

            if action not in ("A", "D"):
                continue

            try:
                shares = float(sh_s or 0)
                price  = float(pr_s or 0)
                total  = round(shares * price, 0)
            except (ValueError, TypeError):
                shares, price, total = 0, 0, 0

            trades.append({
                "Fecha":      date[:10] if date else "N/D",
                "Insider":    owner,
                "Empresa":    company,
                "Ticker":     ticker or "—",
                "Cargo":      role,
                "Tipo":       "🟢 Compra" if action == "A" else "🔴 Venta",
                "Acciones":   f"{int(shares):,}",
                "Precio":     f"${price:.2f}",
                "Valor Total": f"${total:,.0f}" if total > 0 else "N/D",
                "_total_num": total,
            })
    except ET.ParseError:
        pass

    return trades


def fetch_insider_trades() -> list[dict]:
    """
    Obtiene Form-4 recientes del feed Atom de SEC EDGAR.
    Limita a los 20 más recientes para no sobrecargar la API.
    """
    all_trades = []

    try:
        feed_url = (
            "https://www.sec.gov/cgi-bin/browse-edgar"
            "?action=getcurrent&type=4&dateb=&owner=include"
            "&count=40&search_text=&output=atom"
        )
        r = requests.get(feed_url, headers=HEADERS, timeout=30)
        r.raise_for_status()

        root = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        print(f"   ✓ SEC EDGAR: {len(entries)} presentaciones Form-4 encontradas")

        for i, entry in enumerate(entries[:20]):
            try:
                link_el = entry.find("atom:link", ns)
                if link_el is None:
                    continue
                idx_url = link_el.get("href", "")
                if not idx_url:
                    continue

                idx_r = requests.get(idx_url, headers=HEADERS, timeout=10)
                if idx_r.status_code != 200:
                    continue

                xml_paths = re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', idx_r.text)
                if not xml_paths:
                    continue

                xml_url = "https://www.sec.gov" + xml_paths[0]
                xml_r = requests.get(xml_url, headers=HEADERS, timeout=10)
                if xml_r.status_code != 200:
                    continue

                trades = parse_form4_xml(xml_r.content)
                all_trades.extend(trades)
                time.sleep(0.2)  # Respetar rate limits de la SEC

            except Exception:
                continue

        print(f"   → {len(all_trades)} transacciones de insiders obtenidas")

    except Exception as e:
        print(f"   ✗ Error obteniendo datos de insiders: {e}")

    # Ordenar por valor total descendente
    all_trades.sort(key=lambda x: x.get("_total_num", 0), reverse=True)

    # Limpiar campo auxiliar
    for t in all_trades:
        t.pop("_total_num", None)

    return all_trades


# ─────────────────────────────────────────────
# CONSTRUCCIÓN DEL EMAIL HTML
# ─────────────────────────────────────────────

def tabla_html(filas: list[list[str]], cabeceras: list[str], color: str) -> str:
    """Genera una tabla HTML estilizada."""
    ths = "".join(f'<th style="padding:8px 12px;text-align:left;white-space:nowrap">{h}</th>' for h in cabeceras)
    rows_html = ""
    for i, row in enumerate(filas):
        bg = "#f8f9fa" if i % 2 == 0 else "#ffffff"
        tds = "".join(f'<td style="padding:8px 12px;border-bottom:1px solid #dee2e6">{c}</td>' for c in row)
        rows_html += f'<tr style="background:{bg}">{tds}</tr>'

    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px">
        <thead>
            <tr style="background:{color};color:white">
                {ths}
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
    """


def build_email_html(df_congress: pd.DataFrame, insider_trades: list[dict]) -> str | None:
    """
    Construye el cuerpo HTML del email.
    Retorna None si no hay datos que reportar.
    """
    total_c = len(df_congress)
    total_i = len(insider_trades)

    if total_c == 0 and total_i == 0:
        return None

    # ── Sección Congreso ──────────────────────
    congress_section = ""
    if total_c > 0:
        cabeceras = ["Fecha", "Político", "Cámara", "Ticker", "Activo", "Tipo", "Monto estimado"]

        def tipo_badge(val):
            v = str(val).lower()
            if "purchase" in v or "buy" in v or "compra" in v:
                return '<span style="color:#28a745;font-weight:bold">🟢 Compra</span>'
            if "sale" in v or "sell" in v or "venta" in v:
                return '<span style="color:#dc3545;font-weight:bold">🔴 Venta</span>'
            return str(val)

        filas = []
        for _, row in df_congress.head(50).iterrows():
            fecha  = str(row.get("transaction_date", row.get("disclosure_date", "N/D")))[:10]
            nombre = str(row.get("name", "N/D"))
            camara = str(row.get("chamber", "N/D"))
            ticker = str(row.get("ticker", "—")).upper()
            activo = str(row.get("asset_description", "N/D"))[:40]
            tipo   = tipo_badge(row.get("trade_type", "N/D"))
            monto  = str(row.get("amount", "N/D"))
            filas.append([fecha, nombre, camara, ticker, activo, tipo, monto])

        congress_section = f"""
        <h2 style="color:#1f4e79;border-bottom:3px solid #1f4e79;padding-bottom:6px">
            🏛️ Transacciones del Congreso &nbsp;<span style="font-size:14px;font-weight:normal;color:#555">({total_c} nuevas)</span>
        </h2>
        {tabla_html(filas, cabeceras, "#1f4e79")}
        """

    # ── Sección Insiders ──────────────────────
    insiders_section = ""
    if total_i > 0:
        cabeceras_i = ["Fecha", "Insider", "Empresa", "Ticker", "Cargo", "Tipo", "Acciones", "Valor Total"]

        filas_i = []
        for t in insider_trades[:50]:
            filas_i.append([
                t.get("Fecha", "N/D"),
                t.get("Insider", "N/D"),
                t.get("Empresa", "N/D")[:35],
                t.get("Ticker", "—"),
                t.get("Cargo", "N/D"),
                t.get("Tipo", "N/D"),
                t.get("Acciones", "N/D"),
                t.get("Valor Total", "N/D"),
            ])

        insiders_section = f"""
        <h2 style="color:#7b3f00;border-bottom:3px solid #e07b39;padding-bottom:6px;margin-top:32px">
            💼 Insiders Corporativos &nbsp;<span style="font-size:14px;font-weight:normal;color:#555">({total_i} nuevas)</span>
        </h2>
        {tabla_html(filas_i, cabeceras_i, "#c0621a")}
        """

    html = f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,sans-serif;max-width:960px;margin:0 auto;padding:20px;color:#222">

  <!-- ENCABEZADO -->
  <div style="background:linear-gradient(135deg,#1f4e79,#2980b9);color:white;padding:24px 28px;border-radius:10px;margin-bottom:28px">
    <h1 style="margin:0;font-size:22px">📊 US Trade Tracker — Alerta Diaria</h1>
    <p style="margin:8px 0 0;opacity:.9;font-size:14px">
      {TODAY_STR} &nbsp;|&nbsp;
      <strong>{total_c + total_i}</strong> nuevas transacciones detectadas
      ({total_c} del Congreso · {total_i} insiders)
    </p>
  </div>

  {congress_section}
  {insiders_section}

  <!-- PIE DE PÁGINA -->
  <div style="border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;color:#6c757d;font-size:12px">
    <p>⚠️ <strong>Aviso:</strong> Esta información es solo para fines informativos. No constituye asesoramiento financiero.</p>
    <p>📡 Fuentes: House Stock Watcher · Senate Stock Watcher · SEC EDGAR</p>
    <p>🤖 Generado automáticamente por <strong>US Trade Tracker</strong> vía GitHub Actions.</p>
  </div>

</body>
</html>"""

    return html


# ─────────────────────────────────────────────
# ENVÍO DE EMAIL
# ─────────────────────────────────────────────

def send_email(html_body: str, recipient: str, gmail_user: str, gmail_password: str):
    """Envía el email de alerta usando Gmail SMTP."""
    subject = f"📊 US Trade Tracker — {TODAY_STR} — Alerta de nuevas transacciones"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = recipient

    # Versión texto plano (fallback)
    text = MIMEText(
        "Este email requiere un cliente que soporte HTML. "
        "Por favor, ábrelo en Gmail o un cliente moderno.",
        "plain", "utf-8"
    )
    html = MIMEText(html_body, "html", "utf-8")

    msg.attach(text)
    msg.attach(html)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, recipient, msg.as_string())

    print(f"✅ Email enviado exitosamente → {recipient}")


# ─────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"🚀 US Trade Tracker — Alerta Diaria")
    print(f"📅 Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}\n")

    # Leer credenciales de variables de entorno (configuradas en GitHub Secrets)
    gmail_user     = os.environ.get("GMAIL_USER", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    alert_email    = os.environ.get("ALERT_EMAIL", "")

    if not all([gmail_user, gmail_password, alert_email]):
        print("❌ ERROR: Faltan variables de entorno.")
        print("   Necesitas configurar en GitHub Secrets:")
        print("   - GMAIL_USER")
        print("   - GMAIL_APP_PASSWORD")
        print("   - ALERT_EMAIL")
        sys.exit(1)

    # ── Obtener datos ────────────────────────
    print("📥 1/3  Obteniendo trades del Congreso...")
    df_congress = fetch_congressional_trades()

    print("\n📥 2/3  Obteniendo trades de insiders (SEC EDGAR)...")
    insider_trades = fetch_insider_trades()

    # ── Construir email ──────────────────────
    print("\n📧 3/3  Construyendo y enviando email...")
    html_body = build_email_html(df_congress, insider_trades)

    if html_body is None:
        print("ℹ️  No se encontraron nuevas transacciones para hoy. No se envía email.")
        sys.exit(0)

    send_email(html_body, alert_email, gmail_user, gmail_password)
    print("\n✅ Proceso completado exitosamente.")


if __name__ == "__main__":
    main()
