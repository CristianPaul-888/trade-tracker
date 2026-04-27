"""
US Trade Tracker - Dashboard Principal
======================================
Plataforma de monitoreo de transacciones financieras de:
  - Políticos del Congreso de EE.UU. (STOCK Act)
  - Insiders corporativos (SEC EDGAR Form 4)
 
Fuentes de datos: 100% gratuitas y públicas.
"""
 
import streamlit as st
import pandas as pd
import requests
import plotly.express as px
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import re
import time
from bs4 import BeautifulSoup
 
# ─────────────────────────────────────────────
# CONFIGURACIÓN DE LA PÁGINA
# ─────────────────────────────────────────────
 
st.set_page_config(
    page_title="📊 US Trade Tracker",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)
 
# Cabecera de identificación para las APIs (requerido por SEC)
HEADERS = {
    "User-Agent": "USTradeTracker/1.0 (github.com/usuario/trade-tracker; info@ejemplo.com)",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, text/html, */*"
}
 
# Headers alternativos más simples (para S3 y otras APIs)
HEADERS_SIMPLE = {
    "User-Agent": "python-requests/2.31.0"
}
 
 
def safe_fetch_json(url: str, timeout: int = 60, extra_headers: dict | None = None) -> list | dict:
    """
    Descarga JSON de una URL con manejo robusto de errores.
    Intenta con HEADERS normales primero, luego con headers simples.
    Lanza ConnectionError con mensaje útil si falla.
    """
    last_error = ""
    base_headers_list = [HEADERS, HEADERS_SIMPLE]
 
    for attempt, base_hdrs in enumerate(base_headers_list, start=1):
        hdrs = {**base_hdrs, **(extra_headers or {})}
        try:
            r = requests.get(url, headers=hdrs, timeout=timeout)
 
            if r.status_code == 401:
                raise ConnectionError(f"Clave API inválida o falta autorización (HTTP 401) — {url}")
            if r.status_code == 403:
                last_error = f"Acceso denegado (HTTP 403) — {url}"
                continue
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code} — {url}"
                continue
 
            text = r.text.strip()
            if not text:
                last_error = f"Respuesta vacía del servidor (intento {attempt})"
                continue
 
            # S3 y algunos proxies devuelven XML de error con status 200
            if text.startswith("<?xml") or text.startswith("<Error") or text.startswith("<html"):
                try:
                    root = ET.fromstring(text)
                    code = root.findtext("Code", "")
                    msg  = root.findtext("Message", "")
                    last_error = f"Error S3 [{code}]: {msg}"
                except Exception:
                    last_error = f"Respuesta no-JSON: {text[:100]}"
                continue
 
            return r.json()
 
        except ConnectionError:
            raise  # Re-lanzar errores 401 inmediatamente
        except requests.exceptions.Timeout:
            last_error = f"Timeout (intento {attempt})"
        except requests.exceptions.ConnectionError as e:
            last_error = f"Error de conexión: {str(e)[:80]}"
        except ValueError as e:
            last_error = f"JSON inválido: {str(e)[:80]}"
 
    raise ConnectionError(f"No se pudo obtener datos de {url} — {last_error}")
 
# ─────────────────────────────────────────────
# CARGA DE DATOS DEL CONGRESO
# ─────────────────────────────────────────────
 
def _normalize_quiver_congress(data: list) -> pd.DataFrame:
    """
    Normaliza el formato JSON de Quiver Quantitative al esquema común.
    Columnas reales que devuelve la API:
      Representative, BioGuideID, ReportDate, TransactionDate, ticker,
      trade_type, Range, House, amount, party, last_modified, TickerType,
      asset_description, ExcessReturn, PriceChange, SPYChange, chamber, source
    """
    df = pd.DataFrame(data)
 
    rename_map = {
        # Nombre del político
        "Representative":  "name",
        "Politician":      "name",        # nombre alternativo por si cambia
        # Fechas
        "TransactionDate": "transaction_date",   # fecha en que se realizó la operación
        "ReportDate":      "disclosure_date",    # fecha en que entregó la declaración
        "Date":            "transaction_date",   # nombre alternativo
        "Filed":           "disclosure_date",    # nombre alternativo
        # Monto / rango
        "Range":           "amount",
        "Amount":          "amount",
        # Tipo de activo
        "TickerType":      "asset_type",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
 
    # Mapear cámara a español — puede venir como "Chamber" (mayúscula) o "chamber" (minúscula)
    for col in ["Chamber", "chamber"]:
        if col in df.columns:
            df["chamber"] = df[col].map({
                "House":   "Cámara de Representantes",
                "Senate":  "Senado",
                "house":   "Cámara de Representantes",
                "senate":  "Senado",
            }).fillna(df[col])
            if col == "Chamber":
                df = df.drop(columns=["Chamber"])
            break
    else:
        df["chamber"] = "Congreso"
 
    # Mapear partido a nombre completo
    if "party" in df.columns:
        df["party"] = df["party"].map({
            "D":          "Demócrata",
            "R":          "Republicano",
            "I":          "Independiente",
            "Democrat":   "Demócrata",
            "Republican": "Republicano",
        }).fillna(df["party"])
 
    # Eliminar columnas internas que no aportan al usuario
    drop_cols = [c for c in ["BioGuideID", "House", "last_modified",
                              "ExcessReturn", "PriceChange", "SPYChange"] if c in df.columns]
    if drop_cols:
        df = df.drop(columns=drop_cols)
 
    df["source"] = "Político"
    return df
 
 
def _normalize_fmp_congress(data: list, chamber: str) -> pd.DataFrame:
    """
    Normaliza el formato JSON de Financial Modeling Prep (FMP) al esquema común.
    FMP devuelve: firstName, lastName, symbol, type, amount, transactionDate, dateRecieved, assetDescription
    """
    df = pd.DataFrame(data)
 
    if "firstName" in df.columns and "lastName" in df.columns:
        df["name"] = (df["firstName"].fillna("") + " " + df["lastName"].fillna("")).str.strip()
    elif "Politician" in df.columns:
        df = df.rename(columns={"Politician": "name"})
 
    if "symbol" in df.columns:
        df = df.rename(columns={"symbol": "ticker"})
    elif "Ticker" in df.columns:
        df = df.rename(columns={"Ticker": "ticker"})
 
    if "type" in df.columns:
        df = df.rename(columns={"type": "trade_type"})
    elif "Transaction" in df.columns:
        df = df.rename(columns={"Transaction": "trade_type"})
 
    if "transactionDate" in df.columns:
        df = df.rename(columns={"transactionDate": "transaction_date"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "transaction_date"})
 
    if "dateRecieved" in df.columns:
        df = df.rename(columns={"dateRecieved": "disclosure_date"})
 
    if "assetDescription" in df.columns:
        df = df.rename(columns={"assetDescription": "asset_description"})
 
    if "stateDist" in df.columns and "state" not in df.columns:
        df = df.rename(columns={"stateDist": "state"})
 
    df["chamber"] = chamber
    df["source"]  = "Político"
    return df
 
 
@st.cache_data(ttl=7200, show_spinner=False)
def load_congress_trades(quiver_key: str = "", fmp_key: str = "") -> pd.DataFrame:
    """
    Carga trades del Congreso (Senado + Cámara de Representantes).
 
    Orden de fuentes:
      1. Quiver Quantitative API — ambas cámaras + partido, requiere clave gratuita
      2. GitHub Senate Stock Watcher (timothycarambat) — solo Senado, gratis sin clave
      3. Financial Modeling Prep (FMP) — requiere clave gratuita
    TTL: 2 horas
    """
    errors = []
 
    # ── 1. Quiver Quantitative (fuente principal) ─────────────────────────
    # Endpoint gratuito con registro en quiverquant.com
    # Devuelve AMBAS cámaras + partido político en una sola llamada.
    if quiver_key:
        try:
            data = safe_fetch_json(
                "https://api.quiverquant.com/beta/live/congresstrading",
                timeout=45,
                extra_headers={"Authorization": f"Token {quiver_key}"}
            )
            if data and isinstance(data, list) and len(data) > 0:
                return _normalize_quiver_congress(data)
        except Exception as e:
            errors.append(f"Quiver: {str(e)[:120]}")
 
    # ── 2. GitHub Senate Stock Watcher (respaldo gratuito) ───────────────
    # Solo Senado. Mantenido por la comunidad. Sin clave requerida.
    for url in [
        "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
        "https://raw.githubusercontent.com/rdumont/senate-stock-watcher-data/master/aggregate/all_transactions.json",
    ]:
        try:
            data = safe_fetch_json(url, timeout=45)
            if data and isinstance(data, list) and len(data) > 10:
                df = pd.DataFrame(data)
                if "senator" in df.columns:
                    df = df.rename(columns={"senator": "name"})
                elif "owner" in df.columns:
                    df = df.rename(columns={"owner": "name"})
                if "type" in df.columns:
                    df = df.rename(columns={"type": "trade_type"})
                df["chamber"] = "Senado"
                df["source"]  = "Político"
                return df
        except Exception as e:
            errors.append(f"GitHub Senate: {str(e)[:100]}")
 
    # ── 3. Financial Modeling Prep (respaldo con clave) ───────────────────
    if fmp_key:
        parts_fmp = []
        for endpoint, chamber in [
            (f"https://financialmodelingprep.com/api/v4/senate-trading?apikey={fmp_key}", "Senado"),
            (f"https://financialmodelingprep.com/api/v4/house-trading?apikey={fmp_key}", "Cámara de Representantes"),
        ]:
            try:
                data = safe_fetch_json(endpoint, timeout=30)
                if data and isinstance(data, list) and len(data) > 0:
                    parts_fmp.append(_normalize_fmp_congress(data, chamber))
            except Exception as e:
                errors.append(f"FMP {chamber}: {str(e)[:80]}")
        if parts_fmp:
            return pd.concat(parts_fmp, ignore_index=True)
 
    raise ConnectionError("Congreso — todas las fuentes fallaron:\n" + "\n".join(errors))
 
 
def _robust_parse_date(series: pd.Series) -> pd.Series:
    """
    Intenta parsear fechas en múltiples formatos comunes de los datos del Congreso.
    Formatos conocidos:
      - MM/DD/YYYY   (GitHub Senate Stock Watcher)
      - YYYY-MM-DD   (FMP API)
      - MM/DD/YY     (algunos registros históricos)
    """
    # Primero intentar el formato estándar ISO
    result = pd.to_datetime(series, errors="coerce", dayfirst=False)
 
    # Para los que fallaron (NaT), intentar formato MM/DD/YYYY explícitamente
    nat_mask = result.isna()
    if nat_mask.any():
        fallback = pd.to_datetime(
            series[nat_mask].astype(str).str.strip().str.replace(r'\s+', ' ', regex=True),
            format="%m/%d/%Y",
            errors="coerce"
        )
        result = result.copy()
        result[nat_mask] = fallback
 
    # Segundo fallback: MM/DD/YY
    nat_mask2 = result.isna()
    if nat_mask2.any():
        fallback2 = pd.to_datetime(
            series[nat_mask2].astype(str).str.strip(),
            format="%m/%d/%y",
            errors="coerce"
        )
        result = result.copy()
        result[nat_mask2] = fallback2
 
    return result
 
 
def normalize_congressional(df: pd.DataFrame) -> pd.DataFrame:
    """Limpia y normaliza columnas del DataFrame del Congreso."""
    # Parsear fechas con parser robusto multi-formato
    for col in ["transaction_date", "disclosure_date"]:
        if col in df.columns:
            df[col] = _robust_parse_date(df[col].astype(str).replace("--", pd.NaT).replace("N/A", pd.NaT))
 
    # Tipo de operación legible
    if "trade_type" in df.columns:
        def classify(val):
            v = str(val).lower()
            if "purchase" in v or "buy" in v:
                return "Compra"
            if "sale" in v or "sell" in v:
                return "Venta"
            return str(val).title()
        df["trade_type_clean"] = df["trade_type"].apply(classify)
    else:
        df["trade_type_clean"] = "N/D"
 
    # Limpiar ticker
    if "ticker" in df.columns:
        df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
        df = df[~df["ticker"].isin(["", "--", "N/A", "NAN", "NONE", "NA"])]
        df = df[df["ticker"].notna()]
 
    # Limpiar nombre
    if "name" in df.columns:
        df["name"] = df["name"].astype(str).str.strip()
 
    return df
 
 
# ─────────────────────────────────────────────
# CARGA DE DATOS DE INSIDERS — DATAROMA
# ─────────────────────────────────────────────
 
# Headers que simulan un navegador real (necesario para algunos sitios)
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.dataroma.com/",
    "Connection":      "keep-alive",
}
 
 
def _parse_number(text: str) -> float:
    """
    Convierte un string numérico con formato financiero a float.
    Ejemplos: '$1,234,567' → 1234567.0 | '15.3M' → 15300000.0 | '5.4K' → 5400.0
    """
    if not text:
        return 0.0
    t = str(text).strip().replace("$", "").replace(",", "").replace("%", "")
    multiplier = 1.0
    if t.upper().endswith("M"):
        multiplier = 1_000_000
        t = t[:-1]
    elif t.upper().endswith("K"):
        multiplier = 1_000
        t = t[:-1]
    elif t.upper().endswith("B"):
        multiplier = 1_000_000_000
        t = t[:-1]
    try:
        return float(t) * multiplier
    except (ValueError, TypeError):
        return 0.0
 
 
def _extract_ticker_from_cell(cell) -> tuple[str, str]:
    """
    Extrae ticker y nombre de empresa desde una celda HTML de Dataroma.
    Dataroma presenta las empresas en varios formatos:
      - "Apple Inc (AAPL)"        → ('AAPL', 'Apple Inc')
      - Enlace con href ?t=AAPL   → ('AAPL', texto)
      - Solo texto "AAPL"         → ('AAPL', 'AAPL')
    """
    text = cell.get_text(strip=True)
 
    # Formato: "Nombre de Empresa (TICKER)"
    m = re.search(r'\(([A-Z]{1,5}(?:\.[A-Z])?)\)\s*$', text)
    if m:
        ticker  = m.group(1)
        company = text[: m.start()].strip(" -–")
        return ticker, company
 
    # Ticker en parámetro de enlace: href="...?t=AAPL" o "...t=AAPL&..."
    link = cell.find("a")
    if link and link.get("href"):
        href = link.get("href", "")
        m2 = re.search(r'[?&]t=([A-Z]{1,5}(?:\.[A-Z])?)', href)
        if m2:
            return m2.group(1), text
 
    # Si el texto es solo el ticker (todo mayúsculas, 1-5 chars)
    if re.fullmatch(r'[A-Z]{1,5}(?:\.[A-Z])?', text):
        return text, text
 
    return "—", text
 
 
def _scrape_dataroma_page(url: str) -> list[dict]:
    """
    Descarga y parsea UNA página de Dataroma.
    Retorna lista de dicts con trades, o lista vacía si falla.
    """
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    if r.status_code != 200:
        raise ConnectionError(f"HTTP {r.status_code} desde {url}")
 
    soup = BeautifulSoup(r.content, "html.parser")
 
    # ── Buscar la tabla principal ─────────────────────────────────────────
    # Dataroma usa distintos selectores según la sección
    table = None
    for selector_fn in [
        lambda s: s.find("table", {"id": "grid"}),
        lambda s: s.find("div",   {"id": "grid"})   and s.find("div", {"id": "grid"}).find("table"),
        lambda s: s.find("div",   {"id": "main"})   and s.find("div", {"id": "main"}).find("table"),
        lambda s: s.find("div",   {"id": "content"})and s.find("div", {"id": "content"}).find("table"),
        lambda s: s.find("table"),
    ]:
        try:
            result = selector_fn(soup)
            if result:
                table = result if result.name == "table" else result.find("table")
                if table:
                    break
        except Exception:
            continue
 
    if not table:
        raise ValueError(f"No se encontró tabla en: {url}")
 
    rows = table.find_all("tr")
    if len(rows) < 2:
        return []
 
    # ── Detectar cabeceras ────────────────────────────────────────────────
    header_cells = rows[0].find_all(["th", "td"])
    col_names = [c.get_text(strip=True).lower() for c in header_cells]
 
    # ── Mapear columnas por nombre ────────────────────────────────────────
    def col_idx(*keywords) -> int | None:
        for kw in keywords:
            for i, h in enumerate(col_names):
                if kw in h:
                    return i
        return None
 
    idx_date    = col_idx("date", "fecha")
    idx_company = col_idx("company", "stock", "empresa", "ticker")
    idx_insider = col_idx("insider", "name", "nombre")
    idx_title   = col_idx("title", "position", "relation", "role", "cargo")
    idx_action  = col_idx("buy", "sell", "action", "type", "trans")
    idx_shares  = col_idx("share", "qty", "cantidad")
    idx_price   = col_idx("price", "avg", "precio")
    idx_value   = col_idx("value", "total", "valor", "amount")
 
    trades = []
    for row in rows[1:]:
        cells = row.find_all("td")
        n = len(cells)
        if n < 3:
            continue
 
        def get(idx, default="N/D"):
            if idx is not None and idx < n:
                return cells[idx].get_text(strip=True)
            return default
 
        # Extraer ticker y empresa
        comp_idx = idx_company if idx_company is not None else 1
        ticker, company = _extract_ticker_from_cell(cells[comp_idx] if comp_idx < n else cells[0])
 
        # Tipo de operación
        action_raw = get(idx_action, get(4, "N/D"))
        a_low = action_raw.lower()
        if "buy" in a_low or "purchase" in a_low or "compra" in a_low:
            trade_type_clean = "Compra"
        elif "sell" in a_low or "sale" in a_low or "venta" in a_low:
            trade_type_clean = "Venta"
        else:
            trade_type_clean = action_raw.title()
 
        # Valores numéricos
        shares_raw = get(idx_shares, get(5, "0"))
        price_raw  = get(idx_price,  get(6, "0"))
        value_raw  = get(idx_value,  get(7, "0"))
 
        shares = int(_parse_number(shares_raw))
        price  = round(_parse_number(price_raw), 2)
        total  = _parse_number(value_raw)
        if total == 0 and shares > 0 and price > 0:
            total = round(shares * price, 0)
 
        trade = {
            "transaction_date": get(idx_date, get(0, "")),
            "company":          company,
            "ticker":           ticker,
            "name":             get(idx_insider, get(2, "N/D")),
            "title":            get(idx_title,   get(3, "N/D")),
            "trade_type":       action_raw,
            "trade_type_clean": trade_type_clean,
            "shares":           shares,
            "price":            price,
            "total_value":      total,
            "amount":           f"${total:,.0f}" if total > 0 else "N/D",
            "source":           "Insider (Dataroma)",
        }
        trades.append(trade)
 
    return trades
 
 
@st.cache_data(ttl=7200, show_spinner=False)
def load_insider_trades() -> pd.DataFrame:
    """
    Carga datos de insiders corporativos desde Dataroma.com.
    Fuente principal: https://www.dataroma.com/m/ins/ins.php
    Respaldo: SEC EDGAR Form 4 feed.
    TTL: 2 horas.
    """
    # ── 1. Dataroma (fuente principal) ────────────────────────────────────
    dataroma_error = None
    try:
        all_trades: list[dict] = []
 
        # Página principal de insiders de Dataroma
        # (muestra los últimos 100–200 insiders con transacciones recientes)
        for page_url in [
            "https://www.dataroma.com/m/ins/ins.php",
            "https://www.dataroma.com/m/ins/ins.php?po=1",
            "https://www.dataroma.com/m/ins/ins.php?po=2",
        ]:
            try:
                page_trades = _scrape_dataroma_page(page_url)
                if not page_trades:
                    break
                all_trades.extend(page_trades)
                time.sleep(0.5)  # Ser respetuoso con el servidor
            except Exception:
                break
 
        if all_trades:
            df = pd.DataFrame(all_trades)
            if "transaction_date" in df.columns:
                df["transaction_date"] = _robust_parse_date(df["transaction_date"].astype(str))
            df["chamber"] = df["title"].fillna("N/D")
            return df
 
    except Exception as e:
        dataroma_error = str(e)
 
    # ── 2. Respaldo: SEC EDGAR Form 4 Feed ────────────────────────────────
    # Solo si Dataroma falla (por bloqueo, mantenimiento, etc.)
    edgar_trades: list[dict] = []
    try:
        feed_url = (
            "https://www.sec.gov/cgi-bin/browse-edgar"
            "?action=getcurrent&type=4&dateb=&owner=include"
            "&count=40&search_text=&output=atom"
        )
        r = requests.get(feed_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
 
        root_xml = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
 
        for entry in root_xml.findall("atom:entry", ns)[:20]:
            try:
                link_el = entry.find("atom:link", ns)
                if link_el is None:
                    continue
                idx_url = link_el.get("href", "")
                idx_r = requests.get(idx_url, headers=HEADERS, timeout=10)
                if idx_r.status_code != 200:
                    continue
 
                xml_paths = re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', idx_r.text)
                if not xml_paths:
                    continue
 
                xml_r = requests.get("https://www.sec.gov" + xml_paths[0], headers=HEADERS, timeout=10)
                if xml_r.status_code != 200:
                    continue
 
                edgar_trades.extend(_parse_form4_xml(xml_r.content))
                time.sleep(0.15)
            except Exception:
                continue
 
    except Exception:
        pass
 
    if edgar_trades:
        df = pd.DataFrame(edgar_trades)
        if "transaction_date" in df.columns:
            df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
        if dataroma_error:
            st.sidebar.info(f"ℹ️ Insiders: usando SEC EDGAR como respaldo (Dataroma: {dataroma_error[:60]})")
        return df
 
    # ── 3. Nada funcionó ─────────────────────────────────────────────────
    raise ConnectionError(
        "No se pudieron cargar datos de insiders.\n"
        f"Dataroma: {dataroma_error or 'sin datos'}\n"
        "SEC EDGAR: sin resultados"
    )
 
 
def _parse_form4_xml(xml_bytes: bytes) -> list[dict]:
    """Parsea un XML de Formulario 4 de SEC EDGAR (usado como respaldo)."""
    trades = []
    try:
        root = ET.fromstring(xml_bytes)
        company  = root.findtext(".//issuerName", "").strip()
        ticker   = root.findtext(".//issuerTradingSymbol", "").strip().upper()
        owner    = root.findtext(".//rptOwnerName", "").strip()
        is_off   = root.findtext(".//isOfficer", "0")
        title    = root.findtext(".//officerTitle", "").strip()
        is_dir   = root.findtext(".//isDirector", "0")
        role     = title if (is_off == "1" and title) else ("Director" if is_dir == "1" else "Accionista")
 
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
                "name":             owner,
                "company":          company,
                "ticker":           ticker or "—",
                "title":            role,
                "trade_type":       action,
                "trade_type_clean": "Compra" if action == "A" else "Venta",
                "shares":           int(shares),
                "price":            round(price, 2),
                "total_value":      total,
                "transaction_date": date,
                "amount":           f"${total:,.0f}" if total > 0 else "N/D",
                "source":           "Insider (SEC EDGAR)",
                "chamber":          role,
            })
    except ET.ParseError:
        pass
    return trades
 
 
# ─────────────────────────────────────────────
# FUNCIONES DE UTILIDAD
# ─────────────────────────────────────────────
 
def date_cutoff(selection: str) -> pd.Timestamp:
    """Convierte selección de rango a timestamp de inicio."""
    now = pd.Timestamp.now()
    return {
        "Últimos 7 días":   now - pd.Timedelta(days=7),
        "Últimos 30 días":  now - pd.Timedelta(days=30),
        "Últimos 90 días":  now - pd.Timedelta(days=90),
        "Último año":       now - pd.Timedelta(days=365),
        "Todo el historial": pd.Timestamp("2010-01-01"),
    }.get(selection, now - pd.Timedelta(days=30))
 
 
def metric_row(df: pd.DataFrame):
    """Muestra fila de métricas clave."""
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📋 Transacciones totales", f"{len(df):,}")
    if "name" in df.columns:
        c2.metric("👤 Personas únicas", f"{df['name'].nunique():,}")
    if "trade_type_clean" in df.columns:
        buys  = (df["trade_type_clean"] == "Compra").sum()
        sells = (df["trade_type_clean"] == "Venta").sum()
        c3.metric("🟢 Compras", f"{buys:,}")
        c4.metric("🔴 Ventas",  f"{sells:,}")
 
 
# ─────────────────────────────────────────────
# INTERFAZ PRINCIPAL
# ─────────────────────────────────────────────
 
def main():
    # ── Encabezado ──────────────────────────────
    st.title("📊 US Trade Tracker")
    st.markdown(
        "_Monitoreo de compras y ventas en mercados públicos por políticos "
        "e insiders de EE.UU. — Datos 100% gratuitos y públicos._"
    )
    st.divider()
 
    # ── Barra lateral de filtros ─────────────────
    with st.sidebar:
        st.header("🔍 Filtros")
 
        fuentes = st.multiselect(
            "¿Qué quieres ver?",
            ["Políticos (Congreso)", "Insiders Corporativos"],
            default=["Políticos (Congreso)", "Insiders Corporativos"],
        )
 
        rango = st.selectbox(
            "Período de tiempo",
            ["Últimos 7 días", "Últimos 30 días", "Últimos 90 días",
             "Último año", "Todo el historial"],
            index=3,  # Default: Último año (evita perder datos por fechas mal parseadas)
        )
 
        tipo_op = st.multiselect(
            "Tipo de operación",
            ["Compra", "Venta"],
            default=["Compra", "Venta"],
        )
 
        ticker_input = st.text_input("Buscar por ticker (ej: AAPL, NVDA)", "").upper().strip()
        nombre_input = st.text_input("Buscar por nombre", "").strip().lower()
 
        st.divider()
        st.markdown("**📡 Fuentes de datos:**")
        st.markdown("• [Quiver Quantitative](https://www.quiverquant.com/congresstrading/)")
        st.markdown("• [SEC EDGAR Form 4](https://www.sec.gov/cgi-bin/browse-edgar)")
        st.markdown("• [Dataroma](https://www.dataroma.com/m/ins/ins.php)")
        st.divider()
        st.caption("🔄 Datos del Congreso: cada 2h")
        st.caption("🔄 Datos de Insiders: cada 2h")
        st.caption(f"⏰ Sesión iniciada: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
 
    start = date_cutoff(rango)
 
    # ── Tabs ──────────────────────────────────────
    tab_cong, tab_ins, tab_info = st.tabs(
        ["🏛️ Políticos del Congreso", "💼 Insiders Corporativos", "ℹ️ Acerca de"]
    )
 
    # ════════════════════════════════════════════
    # TAB 1 — POLÍTICOS
    # ════════════════════════════════════════════
    with tab_cong:
        if "Políticos (Congreso)" not in fuentes:
            st.info("Activa **Políticos (Congreso)** en el panel de filtros para ver estos datos.")
        else:
            # ── Leer claves API de Streamlit Secrets ─────────────────────────
            # QUIVER_API_KEY: fuente principal (quiverquant.com — gratis con registro)
            # FMP_API_KEY: respaldo opcional (financialmodelingprep.com — gratis con registro)
            quiver_key, fmp_key = "", ""
            try:
                rk = st.secrets.get("QUIVER_API_KEY", "")
                if rk and isinstance(rk, str) and len(rk.strip()) >= 10:
                    quiver_key = rk.strip()
            except Exception:
                quiver_key = ""
            try:
                rk2 = st.secrets.get("FMP_API_KEY", "")
                if rk2 and isinstance(rk2, str) and len(rk2.strip()) >= 20:
                    fmp_key = rk2.strip()
            except Exception:
                fmp_key = ""
 
            with st.spinner("⏳ Cargando datos del Congreso desde Quiver Quantitative..."):
                errors_cong = []
                df_c_raw = pd.DataFrame()
 
                try:
                    df_c_raw = load_congress_trades(quiver_key=quiver_key, fmp_key=fmp_key)
                except Exception as e:
                    errors_cong.append(str(e)[:300])
 
                if df_c_raw.empty:
                    st.error("⚠️ No se pudieron cargar datos del Congreso.")
                    st.info(
                        "Asegúrate de haber configurado tu clave gratuita de Quiver Quantitative en "
                        "Streamlit Secrets. Ve a la pestaña **ℹ️ Acerca de** para instrucciones."
                    )
                    if errors_cong:
                        with st.expander("Ver detalle del error"):
                            st.code("\n".join(errors_cong))
                    cong_ok = False
                else:
                    df_c = normalize_congressional(df_c_raw)
                    cong_ok = True
 
                    # Banner informativo según fuente activa
                    if quiver_key:
                        if "chamber" in df_c.columns:
                            n_senate = df_c["chamber"].str.contains("enado", na=False).sum()
                            n_house  = df_c["chamber"].str.contains("mara|House|house", na=False).sum()
                        else:
                            n_senate = n_house = 0
                        st.success(
                            f"✅ Datos de **Quiver Quantitative** — "
                            f"Senado: {n_senate:,} | Cámara: {n_house:,} transacciones"
                        )
                    else:
                        st.info(
                            "ℹ️ Mostrando datos del **Senado** (respaldo). "
                            "Para ver **ambas cámaras + partidos**, configura tu clave gratuita de "
                            "[Quiver Quantitative](https://www.quiverquant.com/signup) "
                            "en Streamlit Secrets → `QUIVER_API_KEY`. "
                            "Ver instrucciones en la pestaña **ℹ️ Acerca de**."
                        )
                    if errors_cong:
                        st.warning("⚠️ Datos parciales: " + " | ".join(errors_cong))
 
            if cong_ok and not df_c.empty:
                total_loaded = len(df_c)
 
                # ── Diagnóstico: mostrar columnas reales si falta transaction_date ──
                if "transaction_date" not in df_c.columns:
                    st.warning(
                        f"⚠️ La fuente de datos no incluye la columna 'transaction_date'. "
                        f"Columnas disponibles: `{', '.join(df_c.columns.tolist())}`"
                    )
 
                # ── Diagnóstico de fechas (ayuda a detectar problemas de parsing) ──
                if "transaction_date" in df_c.columns:
                    n_nat = df_c["transaction_date"].isna().sum()
                    if n_nat > 0:
                        pct = round(n_nat / total_loaded * 100, 1)
                        st.warning(
                            f"⚠️ {n_nat:,} de {total_loaded:,} registros ({pct}%) tienen fecha sin parsear — "
                            "se mostrarán igual pero no se pueden filtrar por fecha."
                        )
 
                # ── Aplicar filtros ──────────────
                mask = pd.Series([True] * len(df_c), index=df_c.index)
 
                if "transaction_date" in df_c.columns:
                    mask &= df_c["transaction_date"].fillna(pd.Timestamp("2000-01-01")) >= start
 
                if tipo_op:
                    mask &= df_c["trade_type_clean"].isin(tipo_op)
 
                if ticker_input and "ticker" in df_c.columns:
                    mask &= df_c["ticker"] == ticker_input
 
                if nombre_input and "name" in df_c.columns:
                    mask &= df_c["name"].str.lower().str.contains(nombre_input, na=False)
 
                sort_col = "transaction_date" if "transaction_date" in df_c.columns else None
                df_f = df_c[mask].sort_values(sort_col, ascending=False, na_position="last") if sort_col else df_c[mask]
 
                # Si el filtro de fecha dejó 0 resultados pero había datos, avisar al usuario
                if len(df_f) == 0 and total_loaded > 0:
                    st.warning(
                        f"ℹ️ Se cargaron **{total_loaded:,}** registros pero el filtro **{rango}** no mostró ninguno. "
                        "Prueba a seleccionar **'Todo el historial'** en el selector de período."
                    )
 
                metric_row(df_f)
                st.divider()
 
                # ── Gráficos ─────────────────────
                g1, g2 = st.columns(2)
 
                with g1:
                    st.subheader("🏆 Políticos más activos")
                    if "name" in df_f.columns and len(df_f) > 0:
                        top = df_f["name"].value_counts().head(10).reset_index()
                        top.columns = ["Político", "Transacciones"]
                        fig = px.bar(top, x="Transacciones", y="Político",
                                     orientation="h", color="Transacciones",
                                     color_continuous_scale="Blues")
                        fig.update_layout(height=360, showlegend=False,
                                          coloraxis_showscale=False,
                                          yaxis=dict(categoryorder="total ascending"))
                        st.plotly_chart(fig, use_container_width=True)
 
                with g2:
                    st.subheader("📊 Acciones más transaccionadas")
                    if "ticker" in df_f.columns and len(df_f) > 0:
                        top_t = df_f["ticker"].value_counts().head(10).reset_index()
                        top_t.columns = ["Ticker", "Operaciones"]
                        fig2 = px.bar(top_t, x="Ticker", y="Operaciones",
                                      color="Operaciones", color_continuous_scale="Viridis")
                        fig2.update_layout(height=360, showlegend=False,
                                           coloraxis_showscale=False)
                        st.plotly_chart(fig2, use_container_width=True)
 
                # ── Compras vs Ventas por partido ─
                if "party" in df_f.columns and "trade_type_clean" in df_f.columns:
                    st.subheader("🗳️ Compras vs Ventas por partido")
                    party_df = (
                        df_f.groupby(["party", "trade_type_clean"])
                        .size()
                        .reset_index(name="count")
                    )
                    fig3 = px.bar(party_df, x="party", y="count",
                                  color="trade_type_clean",
                                  barmode="group",
                                  color_discrete_map={"Compra": "#28a745", "Venta": "#dc3545"},
                                  labels={"party": "Partido", "count": "Nº operaciones",
                                          "trade_type_clean": "Tipo"})
                    fig3.update_layout(height=320)
                    st.plotly_chart(fig3, use_container_width=True)
 
                # ── Tabla de datos ────────────────
                st.subheader(f"📋 Transacciones ({len(df_f):,} resultados)")
 
                COLS_CONG = {
                    "transaction_date": "Fecha transacción",
                    "disclosure_date":  "Fecha declaración",
                    "name":             "Político",
                    "chamber":          "Cámara",
                    "party":            "Partido",
                    "state":            "Estado",
                    "ticker":           "Ticker",
                    "asset_description":"Activo",
                    "trade_type_clean": "Tipo",
                    "amount":           "Monto estimado",
                }
                show = [c for c in COLS_CONG if c in df_f.columns]
                df_show = df_f[show].rename(columns=COLS_CONG).copy()
 
                # Formatear ambas fechas a DD/MM/YYYY
                for col_fecha in ["Fecha transacción", "Fecha declaración"]:
                    if col_fecha in df_show.columns:
                        df_show[col_fecha] = pd.to_datetime(
                            df_show[col_fecha], errors="coerce"
                        ).dt.strftime("%d/%m/%Y").fillna("—")
 
                st.dataframe(df_show, use_container_width=True, height=420, hide_index=True)
 
    # ════════════════════════════════════════════
    # TAB 2 — INSIDERS
    # ════════════════════════════════════════════
    with tab_ins:
        if "Insiders Corporativos" not in fuentes:
            st.info("Activa **Insiders Corporativos** en el panel de filtros para ver estos datos.")
        else:
            with st.spinner("⏳ Cargando datos de insiders desde SEC EDGAR (puede tardar 30-60 seg la primera vez)..."):
                try:
                    df_ins = load_insider_trades()
                    ins_ok = True
                except Exception as e:
                    st.error(
                        f"⚠️ No se pudieron cargar datos de insiders.\n\n"
                        f"La SEC puede estar temporalmente con alta demanda. "
                        f"Intenta recargar en unos minutos.\n\nDetalle: {str(e)[:200]}"
                    )
                    ins_ok = False
 
            if ins_ok and not df_ins.empty:
                # ── Filtros ───────────────────────
                mask_i = pd.Series([True] * len(df_ins), index=df_ins.index)
 
                if "transaction_date" in df_ins.columns:
                    mask_i &= df_ins["transaction_date"].fillna(pd.Timestamp("2000-01-01")) >= start
 
                if tipo_op and "trade_type_clean" in df_ins.columns:
                    mask_i &= df_ins["trade_type_clean"].isin(tipo_op)
 
                if ticker_input and "ticker" in df_ins.columns:
                    mask_i &= df_ins["ticker"].str.upper() == ticker_input
 
                if nombre_input and "name" in df_ins.columns:
                    mask_i &= df_ins["name"].str.lower().str.contains(nombre_input, na=False)
 
                df_fi = df_ins[mask_i].sort_values("transaction_date", ascending=False, na_position="last")
 
                if len(df_fi) == 0 and len(df_ins) > 0:
                    st.warning(
                        f"ℹ️ Se cargaron **{len(df_ins):,}** registros de insiders pero el filtro "
                        f"**{rango}** no mostró ninguno. "
                        "Prueba a seleccionar **'Todo el historial'** en el selector de período."
                    )
 
                metric_row(df_fi)
                st.divider()
 
                # ── Gráficos ─────────────────────
                g3, g4 = st.columns(2)
 
                with g3:
                    st.subheader("🏢 Empresas con más insiders activos")
                    if "company" in df_fi.columns and len(df_fi) > 0:
                        top_c = df_fi["company"].value_counts().head(10).reset_index()
                        top_c.columns = ["Empresa", "Transacciones"]
                        fig4 = px.bar(top_c, x="Transacciones", y="Empresa",
                                      orientation="h", color="Transacciones",
                                      color_continuous_scale="Oranges")
                        fig4.update_layout(height=360, showlegend=False,
                                           coloraxis_showscale=False,
                                           yaxis=dict(categoryorder="total ascending"))
                        st.plotly_chart(fig4, use_container_width=True)
 
                with g4:
                    st.subheader("💰 Top 10 transacciones por valor")
                    if "total_value" in df_fi.columns and len(df_fi) > 0:
                        top_v = df_fi.nlargest(10, "total_value")[["name", "company", "total_value", "trade_type_clean"]].copy()
                        top_v["label"] = top_v["name"] + " / " + top_v["ticker"] if "ticker" in top_v.columns else top_v["name"]
                        colors = top_v["trade_type_clean"].map({"Compra": "#28a745", "Venta": "#dc3545"}).fillna("#888")
                        fig5 = px.bar(top_v, x="total_value", y="name",
                                      orientation="h",
                                      color="trade_type_clean",
                                      color_discrete_map={"Compra": "#28a745", "Venta": "#dc3545"},
                                      labels={"total_value": "Valor (USD)", "name": "",
                                              "trade_type_clean": "Tipo"})
                        fig5.update_layout(height=360)
                        st.plotly_chart(fig5, use_container_width=True)
 
                # ── Tabla ─────────────────────────
                st.subheader(f"📋 Transacciones recientes ({len(df_fi):,} resultados)")
 
                COLS_INS = {
                    "transaction_date": "Fecha",
                    "name":             "Insider",
                    "company":          "Empresa",
                    "ticker":           "Ticker",
                    "title":            "Cargo",
                    "trade_type_clean": "Tipo",
                    "shares":           "Acciones",
                    "price":            "Precio (USD)",
                    "total_value":      "Valor total (USD)",
                }
                show_i = [c for c in COLS_INS if c in df_fi.columns]
                df_show_i = df_fi[show_i].rename(columns=COLS_INS).copy()
 
                if "Fecha" in df_show_i.columns:
                    df_show_i["Fecha"] = pd.to_datetime(df_show_i["Fecha"]).dt.strftime("%d/%m/%Y")
 
                if "Valor total (USD)" in df_show_i.columns:
                    df_show_i["Valor total (USD)"] = df_show_i["Valor total (USD)"].apply(
                        lambda x: f"${x:,.0f}" if pd.notna(x) and x > 0 else "N/D"
                    )
 
                st.dataframe(df_show_i, use_container_width=True, height=420, hide_index=True)
 
            elif ins_ok:
                st.warning("No se encontraron datos de insiders para el período seleccionado.")
 
    # ════════════════════════════════════════════
    # TAB 3 — INFORMACIÓN
    # ════════════════════════════════════════════
    with tab_info:
        col_a, col_b = st.columns(2)
 
        with col_a:
            st.subheader("📡 Fuentes de datos")
            st.markdown("""
**🏛️ Políticos del Congreso (STOCK Act)**
- Todos los miembros del Congreso están obligados a declarar sus operaciones
  financieras en un plazo de **45 días** según el STOCK Act (2012).
- Fuente principal: **[Quiver Quantitative](https://www.quiverquant.com/congresstrading/)** — incluye Senado + Cámara + partido político *(clave gratuita, ver abajo)*
- Respaldo automático: GitHub Senate Stock Watcher *(sin clave, solo Senado)*
 
**💼 Insiders Corporativos (Form 4)**
- CEOs, directores y ejecutivos deben reportar sus operaciones en **2 días hábiles**.
- Fuente: [Dataroma](https://www.dataroma.com/m/ins/ins.php) + [SEC EDGAR](https://www.sec.gov/cgi-bin/browse-edgar) como respaldo.
            """)
 
            st.subheader("🔑 Configurar clave gratuita de Quiver Quantitative")
            st.markdown("""
Quiver Quantitative es la fuente más completa: incluye **Senado + Cámara** con información de **partido político** de cada legislador.
 
**Pasos para obtener tu clave gratis:**
 
1. Ve a **[quiverquant.com/signup](https://www.quiverquant.com/signup)** y crea una cuenta gratuita *(no requiere tarjeta)*
2. Una vez dentro, ve a tu perfil → **API Key** y copia tu clave
3. En Streamlit Cloud → tu app → ⚙️ **Settings** → **Secrets** → agrega:
```
QUIVER_API_KEY = "tu_clave_aqui"
```
4. Haz clic en **Save** y recarga el dashboard → verás ambas cámaras con datos de partido
 
> El plan gratuito de Quiver permite consultas diarias sin límite estricto para uso personal.
> Sin clave, el dashboard igual funciona con datos del Senado como respaldo automático.
            """)
 
        with col_b:
            st.subheader("⚙️ Frecuencia de actualización")
            st.markdown("""
| Fuente | Actualización del dashboard |
|---|---|
| Quiver Quantitative (Congreso) | Cada 2 horas |
| Dataroma (Insiders) | Cada 2 horas |
| SEC EDGAR (respaldo insiders) | Cada 2 horas |
 
**⏱️ Desfase de datos:**
- Los trades del Congreso pueden aparecer con hasta **45 días** de retraso
  (tiempo legal que tienen para declarar).
- Los insiders corporativos aparecen con **1-2 días** de retraso.
 
**📧 Alertas por email:**
Las alertas diarias se envían automáticamente a las 9 AM ET
a través de GitHub Actions (configurado aparte).
            """)
 
        st.divider()
        st.subheader("⚠️ Aviso legal")
        st.warning(
            "Esta plataforma es solo para fines **informativos y educativos**. "
            "No constituye asesoramiento financiero ni de inversión. "
            "Los datos provienen de fuentes públicas oficiales pero pueden contener errores o desfases. "
            "Siempre verifica la información en las fuentes originales antes de tomar decisiones."
        )
 
 
if __name__ == "__main__":
    main()
