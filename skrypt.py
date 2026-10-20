import os
import requests
import yfinance as yf
import pandas as pd

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Lista walorów do analizy (symbole w Yahoo Finance)
ASSETS = [
    {"name": "MSFT Perp", "ticker": "MSFT", "leverage": 5},
    {"name": "GOOGL Perp", "ticker": "GOOGL", "leverage": 5},
    {"name": "QQQ Perp", "ticker": "QQQ", "leverage": 5}
]

def analyze_asset(asset_info):
    ticker_symbol = asset_info["ticker"]
    name = asset_info["name"]
    leverage = asset_info["leverage"]

    ticker = yf.Ticker(ticker_symbol)
    df = ticker.history(period="1y")

    if df.empty:
        return f"❌ <b>{name}:</b> Błąd pobierania danych.\n"

    # Obliczenie 200 SMA
    df['200SMA'] = df['Close'].rolling(window=200).mean()

    latest_close = df['Close'].iloc[-1]
    latest_sma = df['200SMA'].iloc[-1]
    diff_pct = ((latest_close - latest_sma) / latest_sma) * 100

    # Przykładowe wyliczenie wymaganego depozytu zabezpieczającego (Margin) dla 100 jednostek/kontraktów przy danej dźwigni
    # Wzór: (Cena * Wolumen) / Dźwignia
    position_size = 100  # Przykładowa wielkość pozycji w kontraktach
    required_margin = (latest_close * position_size) / leverage

    # Ocena trendu D1 / W1
    status = "Wzrostowy (Bullish) 🟢" if latest_close >= latest_sma else "Spadkowy / Korekta (Bearish) 🔴"

    msg = (
        f"📌 <b>{name}</b>\n"
        f"• <b>Cena rynkowa (Mark Price):</b> {latest_close:.2f} USD\n"
        f"• <b>Poziom 200 SMA (Stop Loss):</b> {latest_sma:.2f} USD\n"
        f"• <b>Odchylenie od 200 SMA:</b> {diff_pct:+.1f}%\n"
        f"• <b>Łączny depozyt (Margin {leverage}x):</b> {required_margin:,.2f} USD\n"
        f"• <b>Główny trend (D1/W1):</b> {status}\n"
    )
    return msg

def main():
    raport_parts = ["📊 <b>RAPORT SYGNAŁÓW I POTENCJAŁU WALORÓW</b>\n"]
    
    for asset in ASSETS:
        raport_parts.append(analyze_asset(asset))

    pelny_raport = "\n".join(raport_parts)
    wyslij_telegram(pelny_raport)

def wyslij_telegram(tekst):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": tekst, "parse_mode": "HTML"}
    response = requests.post(url, json=payload)
    response.raise_for_status()

if __name__ == "__main__":
    main()
