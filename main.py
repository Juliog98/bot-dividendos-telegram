"""
Bot Radar de Dividendos de CALIDAD
Integración con Render + Flask Web Server para UptimeRobot 24/7
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
import os
import threading
import time
from datetime import date

import yfinance as yf
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- SERVIDOR WEB FLASK PARA UPTIMEROBOT ---
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "🤖 Bot Radar de Dividendos funcionando 24/7", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# --- CARGA DE CONFIGURACIÓN ---
load_dotenv()

TOKEN_TELEGRAM = os.getenv("TELEGRAM_TOKEN")

if not TOKEN_TELEGRAM or TOKEN_TELEGRAM == "TU_BOT_TOKEN_AQUI":
    raise ValueError("❌ ERROR: Debes configurar un TELEGRAM_TOKEN válido.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# --- UNIVERSO DE SEGUIMIENTO ---
UNIVERSO_DIVIDENDOS = [
    # Consumo Defensivo / Salud / Industriales
    "JNJ", "KO", "PEP", "PG", "MMM", "ABBV", "MCD", "TGT", "LMT",
    # REITs / BDC
    "O", "MAIN", "VICI", "ADC", "NNN",
    # Tecnología / Financieras de dividendo
    "MSFT", "AAPL", "TXN", "JPM", "BAC",
]

# --- PARÁMETROS DE CALIDAD ---
MIN_SCORE = 75
MIN_CAP_MERCADO = 10e9
MIN_ANIOS_DIVIDENDO = 10
TOLERANCIA_RECORTE = 0.05
MIN_YIELD = 1.0
MAX_YIELD = 8.0

MAX_PAYOUT = {"normal": 90.0, "reit": 100.0, "financiera": 80.0}
MAX_DEUDA_EBITDA = {"normal": 4.0, "reit": 7.0}

MIN_RATIO_BLOQUE_CLAVE = 0.40
BLOQUES_CLAVE = ("dividendo", "payout", "solidez", "calidad")
MIN_PUNTOS_EVALUABLES = 60

NOMBRES_BLOQUE = {
    "dividendo": "Historial del dividendo",
    "payout": "Seguridad del payout",
    "solidez": "Solidez financiera",
    "calidad": "Calidad del negocio",
    "crecimiento": "Crecimiento",
    "valoracion": "Valoración",
}

CACHE_TTL = 6 * 3600
CONCURRENCIA = 4
_CACHE: dict[str, tuple[float, dict]] = {}


# --- UTILIDADES ---
def _num(info: dict, clave: str) -> float | None:
    v = info.get(clave)
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None

def _pct(info: dict, clave: str) -> float | None:
    v = _num(info, clave)
    return None if v is None else v * 100

def fmt(v: float | None, dec: int = 1, suf: str = "") -> str:
    return "n/d" if v is None else f"{v:.{dec}f}{suf}"

def pts_menor(v: float, cortes: list[float], puntos: list[int]) -> int:
    for c, p in zip(cortes, puntos):
        if v <= c:
            return p
    return 0

def pts_mayor(v: float, cortes: list[float], puntos: list[int]) -> int:
    for c, p in zip(cortes, puntos):
        if v >= c:
            return p
    return 0


# --- CÁLCULO DE DATOS Y EVALUACIÓN ---
def historial_dividendos(ticker: yf.Ticker) -> dict | None:
    divs = ticker.dividends
    if divs is None or divs.empty:
        return None

    anual = divs.groupby(divs.index.year).median()
    anual = anual[anual.index < date.today().year]
    if len(anual) < 2:
        return None

    valores = anual.values
    ventana = anual.tail(MIN_ANIOS_DIVIDENDO + 1)
    cambios = ventana.pct_change().dropna()
    recortes = int((cambios < -TOLERANCIA_RECORTE).sum())

    racha = 0
    for i in range(len(valores) - 1, 0, -1):
        if valores[i] > valores[i - 1] * 1.001:
            racha += 1
        else:
            break

    cagr = None
    if len(valores) >= 6 and valores[-6] > 0:
        cagr = ((valores[-1] / valores[-6]) ** (1 / 5) - 1) * 100

    return {
        "anios_hist": len(anual),
        "recortes": recortes,
        "racha": racha,
        "cagr_div": cagr,
    }


def obtener_datos(simbolo: str) -> dict | None:
    t = yf.Ticker(simbolo)
    info = t.info
    if not info or "shortName" not in info:
        return None

    sector = info.get("sector") or ""
    industria = info.get("industry") or ""
    es_reit = sector.lower() == "real estate" or "reit" in industria.lower()
    es_financiera = sector.lower() == "financial services" and not es_reit
    tipo = "reit" if es_reit else ("financiera" if es_financiera else "normal")

    precio = _num(info, "currentPrice") or _num(info, "regularMarketPrice")

    div_anual = _num(info, "dividendRate")
    if div_anual and precio:
        yield_pct = div_anual / precio * 100
    else:
        dy = _num(info, "dividendYield")
        yield_pct = None if dy is None else (dy * 100 if dy < 0.25 else dy)

    acciones = _num(info, "sharesOutstanding")
    fcf = _num(info, "freeCashflow")
    ocf = _num(info, "operatingCashflow")
    dividendos_totales = div_anual * acciones if div_anual and acciones else None

    payout_gaap = _pct(info, "payoutRatio")
    payout_fcf = (dividendos_totales / fcf * 100) if dividendos_totales and fcf and fcf > 0 else None
    payout_ocf = (dividendos_totales / ocf * 100) if dividendos_totales and ocf and ocf > 0 else None

    if tipo == "normal":
        payout_ref, payout_base = payout_fcf, "FCF"
    elif tipo == "reit":
        payout_ref, payout_base = payout_ocf, "flujo operativo"
    else:
        payout_ref, payout_base = payout_gaap, "beneficio"
        
    if payout_ref is None and payout_gaap is not None:
        payout_ref, payout_base = payout_gaap, "beneficio"

    deuda = _num(info, "totalDebt")
    caja = _num(info, "totalCash") or 0.0
    ebitda = _num(info, "ebitda")
    deuda_ebitda = None
    if deuda is not None and ebitda and ebitda > 0:
        deuda_ebitda = max(0.0, deuda - caja) / ebitda

    per = next((v for v in (_num(info, "forwardPE"), _num(info, "trailingPE")) if v and v > 0), None)

    roe = _pct(info, "returnOnEquity")
    if roe is not None and roe > 100:
        roe = None

    hist = historial_dividendos(t) or {}

    return {
        "ticker": simbolo.upper(),
        "nombre": info.get("longName") or info.get("shortName") or simbolo,
        "sector": sector,
        "tipo": tipo,
        "precio": precio,
        "cap": _num(info, "marketCap"),
        "yield_pct": yield_pct,
        "yield_medio_5a": _num(info, "fiveYearAvgDividendYield"),
        "payout_ref": payout_ref,
        "payout_base": payout_base,
        "fcf": fcf,
        "ebitda": ebitda,
        "deuda_ebitda": deuda_ebitda,
        "per": per,
        "roe": roe,
        "margen_op": _pct(info, "operatingMargins"),
        "crec_ingresos": _pct(info, "revenueGrowth"),
        "crec_beneficios": _pct(info, "earningsGrowth"),
        "anios_hist": hist.get("anios_hist"),
        "recortes": hist.get("recortes"),
        "racha": hist.get("racha"),
        "cagr_div": hist.get("cagr_div"),
    }


def evaluar(d: dict) -> dict:
    tipo = d["tipo"]
    motivos: list[str] = []

    if d["cap"] is None or d["cap"] < MIN_CAP_MERCADO:
        motivos.append(f"Capitalización inferior a ${MIN_CAP_MERCADO / 1e9:.0f}B")

    if d["anios_hist"] is None:
        motivos.append("Sin historial de dividendos suficiente")
    else:
        if d["anios_hist"] < MIN_ANIOS_DIVIDENDO:
            motivos.append(f"Historial de dividendo corto ({d['anios_hist']} años)")
        if d["recortes"] and d["recortes"] > 0:
            motivos.append(f"Recortó el dividendo en los últimos {MIN_ANIOS_DIVIDENDO} años")

    y = d["yield_pct"]
    if y is None or y < MIN_YIELD:
        motivos.append(f"Yield {fmt(y, 2, '%')} inferior al mínimo ({MIN_YIELD}%)")
    elif y > MAX_YIELD:
        motivos.append(f"Yield {y:.1f}% sospechosamente alto (posible trampa)")

    if tipo == "normal" and d["fcf"] is not None and d["fcf"] <= 0:
        motivos.append("Flujo de caja libre negativo")

    if d["payout_ref"] is not None and d["payout_ref"] > MAX_PAYOUT[tipo]:
        motivos.append(f"Payout sobre {d['payout_base']} insostenible ({d['payout_ref']:.0f}% > {MAX_PAYOUT[tipo]:.0f}%)")

    if tipo == "normal" and d["ebitda"] is not None and d["ebitda"] <= 0:
        motivos.append("EBITDA no positivo")
    if tipo in MAX_DEUDA_EBITDA and d["deuda_ebitda"] is not None and d["deuda_ebitda"] > MAX_DEUDA_EBITDA[tipo]:
        motivos.append(f"Deuda neta/EBITDA excesiva ({d['deuda_ebitda']:.1f}x > {MAX_DEUDA_EBITDA[tipo]:.0f}x)")

    if tipo != "reit" and d["roe"] is not None and d["roe"] < 0:
        motivos.append("ROE negativo")

    puntos: dict[str, list[int]] = {b: [0, 0] for b in NOMBRES_BLOQUE}

    def sumar(bloque: str, pts: int | None, maximo: int) -> None:
        if pts is None:
            return
        puntos[bloque][0] += pts
        puntos[bloque][1] += maximo

    if d["racha"] is not None:
        sumar("dividendo", pts_mayor(d["racha"], [25, 10, 5], [10, 7, 4]), 10)
    if d["cagr_div"] is not None:
        sumar("dividendo", pts_mayor(d["cagr_div"], [7, 4, 2], [10, 7, 4]), 10)

    cortes_payout = {"normal": [50, 65, 80], "reit": [70, 80, 90], "financiera": [40, 55, 70]}[tipo]
    if d["payout_ref"] is not None:
        sumar("payout", pts_menor(d["payout_ref"], cortes_payout, [20, 15, 8]), 20)

    if d["deuda_ebitda"] is not None and tipo != "financiera":
        p = pts_menor(d["deuda_ebitda"], [5, 6, 7], [15, 10, 5]) if tipo == "reit" else pts_menor(d["deuda_ebitda"], [1.5, 2.5, 3.5], [15, 11, 6])
        sumar("solidez", p, 15)

    if tipo != "reit":
        if d["roe"] is not None:
            cortes_roe = [14, 11, 8] if tipo == "financiera" else [20, 15, 10]
            sumar("calidad", pts_mayor(d["roe"], cortes_roe, [10, 7, 4]), 10)
        if tipo == "normal" and d["margen_op"] is not None:
            sumar("calidad", pts_mayor(d["margen_op"], [25, 15, 10], [10, 7, 4]), 10)

    if d["crec_ingresos"] is not None:
        sumar("crecimiento", pts_mayor(d["crec_ingresos"], [5, 2, 0], [5, 3, 1]), 5)
    if d["crec_beneficios"] is not None:
        sumar("crecimiento", pts_mayor(d["crec_beneficios"], [8, 0], [5, 3]), 5)

    if d["per"] is not None and tipo != "reit":
        sumar("valoracion", pts_menor(d["per"], [20, 25, 30], [8, 5, 2]), 8)
    if y and d["yield_medio_5a"]:
        sumar("valoracion", pts_mayor(y / d["yield_medio_5a"], [1.0, 0.9], [7, 4]), 7)

    obtenido = sum(v[0] for v in puntos.values())
    maximo = sum(v[1] for v in puntos.values())

    if maximo < MIN_PUNTOS_EVALUABLES:
        score = 0
        motivos.append("Datos insuficientes para evaluar con fiabilidad")
    else:
        score = round(obtenido / maximo * 100)
        for b in BLOQUES_CLAVE:
            obt, mx = puntos[b]
            if mx and obt / mx < MIN_RATIO_BLOQUE_CLAVE:
                motivos.append(f"Bloque débil: {NOMBRES_BLOQUE[b]} ({obt}/{mx})")
        if score < MIN_SCORE:
            motivos.append(f"Score {score} inferior al mínimo ({MIN_SCORE})")

    return {**d, "score": score, "puntos": puntos, "motivos": motivos, "aprobada": not motivos}


def evaluar_empresa(simbolo: str) -> dict | None:
    simbolo = simbolo.upper()
    hit = _CACHE.get(simbolo)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        datos = obtener_datos(simbolo)
        if not datos:
            return None
        resultado = evaluar(datos)
    except Exception as e:
        logging.error(f"Error evaluando {simbolo}: {e}")
        return None
    _CACHE[simbolo] = (time.time(), resultado)
    return resultado


# --- HANDLERS DE TELEGRAM ---
async def responder(update: Update, texto: str) -> None:
    trozos, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > 4000:
            trozos.append(actual)
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        trozos.append(actual)
    for trozo in trozos:
        await update.message.reply_text(trozo, parse_mode=ParseMode.HTML)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mensaje = (
        "🤖 <b>Radar de Dividendos de CALIDAD (24/7 en la Nube)</b>\n\n"
        "Comandos:\n"
        "• <code>/radar</code> - Escanea el universo de acciones.\n"
        "• <code>/analizar TICKER</code> - Análisis detallado.\n"
        "• <code>/universo</code> - Lista de acciones bajo seguimiento.\n\n"
        f"📌 Tu Chat ID es: <code>{chat_id}</code>"
    )
    await responder(update, mensaje)


async def analizar_individual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await responder(update, "⚠️ Indica un ticker. Ejemplo: <code>/analizar O</code>")
        return

    ticker = html.escape(context.args[0].upper())
    await responder(update, f"🔍 Analizando <b>{ticker}</b>...")

    r = await asyncio.to_thread(evaluar_empresa, context.args[0])
    if not r:
        await responder(update, f"❌ No se pudieron obtener datos para <code>{ticker}</code>.")
        return

    tipo_txt = {"normal": "Empresa", "reit": "REIT", "financiera": "Financiera"}[r["tipo"]]
    desglose = "\n".join(
        f"• {NOMBRES_BLOQUE[b]}: <code>{obt}/{mx}</code>"
        for b, (obt, mx) in r["puntos"].items() if mx
    )
    veredicto = "✅ <b>APTA: pasa todos los filtros de calidad</b>" if r["aprobada"] \
        else "❌ <b>DESCARTADA</b>\n" + "\n".join(f"  – {html.escape(m)}" for m in r["motivos"])

    resumen = (
        f"📊 <b>{html.escape(r['nombre'])} ({r['ticker']})</b>\n"
        f"Tipo: <code>{tipo_txt}</code> | Precio: <code>${fmt(r['precio'], 2)}</code> | Cap.: <code>${fmt((r['cap'] or 0) / 1e9, 0)}B</code>\n\n"
        f"<b>Dividendo</b>\n"
        f"Yield: <code>{fmt(r['yield_pct'], 2, '%')}</code> (media 5a: <code>{fmt(r['yield_medio_5a'], 2, '%')}</code>)\n"
        f"Payout s/{html.escape(r['payout_base'])}: <code>{fmt(r['payout_ref'], 0, '%')}</code>\n"
        f"Años subiendo: <code>{r['racha'] if r['racha'] is not None else 'n/d'}</code> | CAGR 5a: <code>{fmt(r['cagr_div'], 1, '%')}</code>\n\n"
        f"<b>Negocio</b>\n"
        f"Deuda neta/EBITDA: <code>{fmt(r['deuda_ebitda'], 1, 'x')}</code> | PER: <code>{fmt(r['per'], 1)}</code>\n"
        f"ROE: <code>{fmt(r['roe'], 1, '%')}</code> | Margen op.: <code>{fmt(r['margen_op'], 1, '%')}</code>\n\n"
        f"<b>Desglose del score</b>\n{desglose}\n\n"
        f"🏆 <b>SCORE: {r['score']}/100</b>\n{veredicto}"
    )
    await responder(update, resumen)


async def ejecutar_radar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await responder(update, f"🛰️ <b>Iniciando Radar de Calidad...</b>\nEscaneando {len(UNIVERSO_DIVIDENDOS)} empresas...")

    sem = asyncio.Semaphore(CONCURRENCIA)

    async def tarea(ticker: str):
        async with sem:
            return await asyncio.to_thread(evaluar_empresa, ticker)

    resultados = await asyncio.gather(*(tarea(t) for t in UNIVERSO_DIVIDENDOS))
    validos = [r for r in resultados if r]
    aprobadas = sorted((r for r in validos if r["aprobada"]), key=lambda r: r["score"], reverse=True)

    if not aprobadas:
        await responder(update, "🔍 Ninguna empresa supera los filtros de calidad actualmente.")
        return

    mensaje = f"🎯 <b>EMPRESAS DE CALIDAD DETECTADAS (Score ≥ {MIN_SCORE}):</b>\n\n"
    for r in aprobadas:
        emoji = "🟢" if r["score"] >= 85 else "🟡"
        mensaje += (
            f"{emoji} <b>{r['ticker']}</b> ({html.escape(r['nombre'])})\n"
            f"   • Score: <b>{r['score']}/100</b>\n"
            f"   • Yield: <code>{fmt(r['yield_pct'], 2, '%')}</code> | Payout s/{html.escape(r['payout_base'])}: <code>{fmt(r['payout_ref'], 0, '%')}</code>\n"
            f"   • PER: <code>{fmt(r['per'], 1)}</code> | Deuda/EBITDA: <code>{fmt(r['deuda_ebitda'], 1, 'x')}</code>\n\n"
        )

    await responder(update, mensaje)


async def mostrar_universo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lista = ", ".join(f"<code>{t}</code>" for t in UNIVERSO_DIVIDENDOS)
    await responder(update, f"📋 <b>Empresas en el Radar ({len(UNIVERSO_DIVIDENDOS)}):</b>\n\n{lista}")


if __name__ == "__main__":
    # Iniciar servidor Flask en hilo secundario
    threading.Thread(target=run_flask, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN_TELEGRAM).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analizar", analizar_individual))
    app.add_handler(CommandHandler("radar", ejecutar_radar))
    app.add_handler(CommandHandler("universo", mostrar_universo))

    print("🤖 Bot Radar de Calidad listo con servidor Flask habilitado...")
    app.run_polling()