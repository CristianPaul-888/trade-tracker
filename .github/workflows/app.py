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

# ─────────────────────────────────────────────
# CARGA DE DATOS DEL CONGRESO
# ─────────────────────────────────────────────

@st.cache_data(ttl=14400, show_spinner=False)
def load_house_trades():
    """
    Carga trades de la Cámara de Representantes.
    Fuente: House Stock Watcher (S3 público, datos del STOCK Act)
    TTL: 4 horas
    """
    url = "https://house-stock-watcher-data.s3-us-east-2.amazonaws.com/data/all_transactions.json"
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    df = pd.DataFrame(r.json())

    # Normalizar nombre del político
    if "representative" in df.columns:
        df = df.rename(columns={"representative": "name"})
    elif "owner" in df.columns:
        df = df.rename(columns={"owner": "name"})

    # Normalizar tipo de transacción
    if "type" in df.columns:
        df = df.rename(columns={"type": "trade_type"})

    df["chamber"] = "Cámara de Representantes"
    df["source"] = "Político"
    return df


@st.cache_data(ttl=14400, show_spinner=False)
def load_senate_trades():
    """
    Carga trades del Senado.
    Fuente: Senate Stock Watcher (S3 público, datos del STOCK Act)
    TTL: 4 horas
    """
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
    df["source"] = "Político"
    return df


def normalize_congressional(df: pd.DataFrame) -> pd.DataFrame:
    """Limpia y normaliza columnas del DataFrame del Congreso."""
    # Parsear fechas
    for col in ["transaction_date", "disclosure_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", dayfirst=False)

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
# CARGA DE DATOS DE INSIDERS (SEC EDGAR)
# ─────────────────────────────────────────────

def parse_form4_xml(xml_bytes: bytes) -> list[dict]:
    """
    Extrae datos de transacciones de un archivo XML de Formulario 4.
    Retorna lista de dicts con los campos relevantes.
    """
    trades = []
    try:
        root = ET.fromstring(xml_bytes)

        company   = root.findtext(".//issuerName", "").strip()
        ticker    = root.findtext(".//issuerTradingSymbol", "").strip().upper()
        owner     = root.findtext(".//rptOwnerName", "").strip()
        is_off    = root.findtext(".//isOfficer", "0")
        title     = root.findtext(".//officerTitle", "").strip()
        is_dir    = root.findtext(".//isDirector", "0")

        role = title if (is_off == "1" and title) else ("Director" if is_dir == "1" else "Accionista")

        for tx in root.findall(".//nonDerivativeTransaction"):
            date      = tx.findtext("transactionDate/value", "").strip()
            shares_s  = tx.findtext("transactionAmounts/transactionShares/value", "0")
            price_s   = tx.findtext("transactionAmounts/transactionPricePerShare/value", "0")
            action    = tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value", "").strip()

            if action not in ("A", "D"):
                continue

            try:
                shares = float(shares_s or 0)
                price  = float(price_s or 0)
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
                "source":           "Insider",
                "chamber":          role,
            })
    except ET.ParseError:
        pass
    return trades


@st.cache_data(ttl=7200, show_spinner=False)
def load_insider_trades() -> pd.DataFrame:
    """
    Obtiene los 40 Form-4 más recientes vía el feed Atom de EDGAR.
    Parsea cada XML para extraer operaciones.
    TTL: 2 horas
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

        for entry in entries[:30]:  # Limitar para evitar timeouts
            try:
                link_el = entry.find("atom:link", ns)
                if link_el is None:
                    continue
                idx_url = link_el.get("href", "")
                if not idx_url:
                    continue

                # Descargar índice HTML de la presentación
                idx_r = requests.get(idx_url, headers=HEADERS, timeout=10)
                if idx_r.status_code != 200:
                    continue

                # Buscar enlace al XML del Form 4
                xml_paths = re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', idx_r.text)
                if not xml_paths:
                    continue

                xml_url = "https://www.sec.gov" + xml_paths[0]
                xml_r = requests.get(xml_url, headers=HEADERS, timeout=10)
                if xml_r.status_code != 200:
                    continue

                trades = parse_form4_xml(xml_r.content)
                all_trades.extend(trades)

                time.sleep(0.15)  # Respetar rate limits de la SEC

            except Exception:
                continue

    except Exception as e:
        st.sidebar.warning(f"⚠️ Error cargando insiders: {str(e)[:80]}")

    if not all_trades:
        return pd.DataFrame()

    df = pd.DataFrame(all_trades)
    if "transaction_date" in df.columns:
        df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
    return df


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
            index=1,
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
        st.markdown("• [House Stock Watcher](https://housestockwatcher.com)")
        st.markdown("• [Senate Stock Watcher](https://senatestockwatcher.com)")
        st.markdown("• [SEC EDGAR Form 4](https://www.sec.gov/cgi-bin/browse-edgar)")
        st.divider()
        st.caption("🔄 Datos del Congreso: cada 4h")
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
            with st.spinner("⏳ Cargando datos del Congreso (primera carga puede tardar ~15 seg)..."):
                try:
                    df_h = load_house_trades()
                    df_s = load_senate_trades()
                    df_c = normalize_congressional(pd.concat([df_h, df_s], ignore_index=True))
                    cong_ok = True
                except Exception as e:
                    st.error(f"Error al cargar datos del Congreso: {e}")
                    cong_ok = False

            if cong_ok and not df_c.empty:
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

                df_f = df_c[mask].sort_values("transaction_date", ascending=False)

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
                    "transaction_date": "Fecha",
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

                if "Fecha" in df_show.columns:
                    df_show["Fecha"] = pd.to_datetime(df_show["Fecha"]).dt.strftime("%d/%m/%Y")

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
                    st.error(f"Error al cargar datos de insiders: {e}")
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

                df_fi = df_ins[mask_i].sort_values("transaction_date", ascending=False)

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
- Los datos son obtenidos de:
  - [House Stock Watcher](https://housestockwatcher.com) — Cámara de Representantes
  - [Senate Stock Watcher](https://senatestockwatcher.com) — Senado
- Cobertura: desde 2012 hasta hoy

**💼 Insiders Corporativos (Form 4)**
- CEOs, directores y ejecutivos deben reportar sus operaciones en **2 días hábiles**.
- Fuente oficial: [SEC EDGAR](https://www.sec.gov/cgi-bin/browse-edgar)
- Los datos corresponden a los formularios más recientes disponibles.
            """)

        with col_b:
            st.subheader("⚙️ Frecuencia de actualización")
            st.markdown("""
| Fuente | Actualización del dashboard |
|---|---|
| Cámara de Representantes | Cada 4 horas |
| Senado | Cada 4 horas |
| Insiders (Form 4) | Cada 2 horas |

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
