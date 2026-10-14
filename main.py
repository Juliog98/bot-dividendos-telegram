import logging
import asyncio
import yfinance as yf
import pandas as pd
import numpy as np
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Configuración de Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN_TELEGRAM = "TU_BOT_TOKEN_AQUI"

def analizar_accion(ticker_symbol: str) -> dict:
    """
    Descarga métricas financieras y calcula el score de 0 a 100
    adaptado según el sector (REIT vs Empresas Convencionales).
    """
    ticker = yf.Ticker(ticker_symbol)
    info = ticker.info
    
    if not info or 'shortName' not in info:
        return {"error": f"No se encontraron datos para el ticker '{ticker_symbol}'."}

    sector = info.get('sector', '')
    es_reit = sector.lower() == 'real estate' or 'reit' in info.get('industry', '').lower()

    # --- Extraer Métricas Básicas ---
    precio_actual = info.get('currentPrice') or info.get('regularMarketPrice', 0.0)
    div_yield = info.get('dividendYield', 0.0)  # Decimal (ej: 0.04 = 4%)
    if div_yield is None:
        div_yield = 0.0
    div_yield_pct = div_yield * 100

    payout_ratio = info.get('payoutRatio', 0.0)  # Payout sobre Beneficio Neto
    if payout_ratio is None:
        payout_ratio = 0.0
    payout_pct = payout_ratio * 100

    pe_ratio = info.get('trailingPE', 0.0)
    forward_pe = info.get('forwardPE', 0.0)
    deuda_ebitda = info.get('debtToEquity', 0.0) / 100.0  # Proxy de estructura financiera
    
    # --- Puntuación Adaptada (Score de 0 a 100) ---
    score = 0
    desglose = []
    
    # 1. Seguridad y Cobertura del Dividendo (Max 25 pts)
    puntos_seguridad = 0
    if es_reit:
        # En REITs el payout sobre beneficio neto suele ser > 100%, evaluamos margen razonable
        if 60 <= payout_pct <= 90:
            puntos_seguridad = 25
            desglose.append("✅ Cobertura dividendo (REIT): Payout adecuado para el sector (25/25)")
        elif payout_pct < 60 or (90 < payout_pct <= 105):
            puntos_seguridad = 15
            desglose.append("⚠️ Cobertura dividendo (REIT): Payout elevado o atípico (15/25)")
        else:
            desglose.append("🚨 Cobertura dividendo (REIT): Payout en riesgo (>105%) (0/25)")
    else:
        # Empresas Convencionales
        if payout_pct > 0 and payout_pct <= 60:
            puntos_seguridad = 25
            desglose.append("✅ Cobertura dividendo: Payout muy seguro (<60%) (25/25)")
        elif 60 < payout_pct <= 80:
            puntos_seguridad = 15
            desglose.append("⚠️ Cobertura dividendo: Payout moderado (60-80%) (15/25)")
        else:
            desglose.append("🚨 Cobertura dividendo: Payout elevado o insostenible (>80%) (0/25)")
    score += puntos_seguridad

    # 2. Deuda y Solidez Financiera (Max 15 pts)
    puntos_deuda = 0
    if deuda_ebitda < 1.5:
        puntos_deuda = 15
        desglose.append("✅ Solidez financiera: Deuda baja / sostenible (15/15)")
    elif 1.5 <= deuda_ebitda <= 3.5:
        puntos_deuda = 10
        desglose.append("⚠️ Solidez financiera: Nivel de deuda moderado (10/15)")
    else:
        desglose.append("🚨 Solidez financiera: Alto endeudamiento (0/15)")
    score += puntos_deuda

    # 3. Crecimiento (Max 15 pts)
    puntos_crecimiento = 0
    earnings_growth = info.get('earningsGrowth', 0.0)
    if earnings_growth and earnings_growth > 0.05:
        puntos_crecimiento = 15
        desglose.append("✅ Crecimiento estimado del negocio positivo (>5%) (15/15)")
    elif earnings_growth and earnings_growth > 0:
        puntos_crecimiento = 8
        desglose.append("⚠️ Crecimiento modesto del negocio (8/15)")
    else:
        desglose.append("🚨 Crecimiento estancado o negativo (0/15)")
    score += puntos_crecimiento

    # 4. Valoración e Histórico (Max 20 pts)
    puntos_val = 0
    if pe_ratio > 0 and pe_ratio < 20:
        puntos_val = 20
        desglose.append("✅ Valoración atractiva por múltiplos (20/20)")
    elif 20 <= pe_ratio <= 28:
        puntos_val = 12
        desglose.append("⚠️ Valoración dentro del promedio razonable (12/20)")
    else:
        desglose.append("🚨 Valoración exigente / Cara (0/20)")
    score += puntos_val

    # 5. Calidad del Negocio / Moat (Max 10 pts)
    puntos_calidad = 0
    roe = info.get('returnOnEquity', 0.0)
    if roe and roe > 0.12:
        puntos_calidad = 10
        desglose.append("✅ Alta rentabilidad sobre el capital (ROE > 12%) (10/10)")
    else:
        puntos_calidad = 5
        desglose.append("⚠️ Rentabilidad sobre el capital moderada/baja (5/10)")
    score += puntos_calidad

    # 6. Consenso de Analistas (Max 5 pts)
    puntos_analistas = 0
    rec = info.get('recommendationKey', '').lower()
    if rec in ['buy', 'strong_buy']:
        puntos_analistas = 5
        desglose.append("✅ Consenso de analistas: Compra / Compra Fuerte (5/5)")
    else:
        puntos_analistas = 2
        desglose.append("⚠️ Consenso de analistas: Mantener / Neutral (2/5)")
    score += puntos_analistas

    # 7. Rentabilidad Total Potencial (Max 10 pts)
    # Estimación simplificada de CAGR: Dividend Yield + Crecimiento Estimado
    cagr_est = div_yield_pct + ((earnings_growth or 0) * 100)
    puntos_cagr = 0
    if cagr_est >= 8.0:
        puntos_cagr = 10
        desglose.append(f"✅ Rentabilidad potencial estimada atractiva ({cagr_est:.1f}%) (10/10)")
    else:
        puntos_cagr = 4
        desglose.append(f"⚠️ Rentabilidad potencial moderada ({cagr_est:.1f}%) (4/10)")
    score += puntos_cagr

    return {
        "nombre": info.get('longName', ticker_symbol),
        "ticker": ticker_symbol.upper(),
        "sector": "REIT" if es_reit else sector,
        "precio": precio_actual,
        "yield_pct": div_yield_pct,
        "payout_pct": payout_pct,
        "score": score,
        "desglose": desglose,
        "target_price": info.get('targetMeanPrice', "N/A")
    }

# --- COMANDOS DEL BOT DE TELEGRAM ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "👋 **Bot Analizador de Acciones de Dividendos**\n\n"
        "Usa el comando `/analizar <TICKER>` para evaluar una empresa.\n"
        "Ejemplos:\n"
        "• `/analizar JNJ` (Johnson & Johnson)\n"
        "• `/analizar O` (Realty Income - REIT)\n"
        "• `/analizar KO` (Coca-Cola)"
    )
    await update.message.reply_text(mensaje, parse_mode='Markdown')

async def analizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Por favor indica un ticker. Ejemplo: `/analizar PEP`", parse_mode='Markdown')
        return

    ticker_input = context.args[0].upper()
    await update.message.reply_text(f"🔍 Analizando **{ticker_input}**...", parse_mode='Markdown')

    try:
        data = analizar_accion(ticker_input)
        
        if "error" in data:
            await update.message.reply_text(f"❌ {data['error']}")
            return

        # Clasificación según el score total
        score = data['score']
        if score >= 80:
            clasificacion = "🟢 Excelente Inversión a Largo Plazo"
        elif score >= 60:
            clasificacion = "🟡 Inversión Aceptable / Requiere Seguimiento"
        else:
            clasificacion = "🔴 Inversión de Alto Riesgo / Poco Atractiva"

        desglose_texto = "\n".join([f"• {item}" for item in data['desglose']])

        resumen = (
            f"📊 **ANÁLISIS DE DIVIDENDOS: {data['nombre']} ({data['ticker']})**\n"
            f" Sector: `{data['sector']}`\n"
            f" Precio Actual: `${data['precio']}`\n"
            f" Dividend Yield: `{data['yield_pct']:.2f}%`\n"
            f" Payout Ratio: `{data['payout_pct']:.2f}%`\n"
            f" Precio Objetivo Analistas: `${data['target_price']}`\n\n"
            f"🏆 **PUNTUACIÓN FINAL: {score}/100**\n"
            f"**Clasificación:** {clasificacion}\n\n"
            f"📋 **Desglose de Factores:**\n"
            f"{desglose_texto}\n\n"
            f"⚠️ *Nota: Los datos son informativos y dependen de la disponibilidad de las fuentes.*"
        )

        await update.message.reply_text(resumen, parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"❌ Ocurrió un error procesando la consulta: {str(e)}")

# --- INICIALIZACIÓN DEL BOT ---
if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN_TELEGRAM).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analizar", analizar))
    
    print("🤖 Bot listo y escuchando en Telegram...")
    app.run_polling()