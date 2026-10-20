import os
import requests
import yfinance as yf
import pandas as pd

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def analyze_qqq():
    # Pobieramy dane dla QQQ (ostatni rok, aby wyliczyć 200 SMA)
    ticker = yf.Ticker("QQQ")
    df = ticker.history(period="1y")
    
    if df.empty:
        return "Błąd: Nie udało się pobrać danych dla QQQ."

    df['200SMA'] = df['Close'].rolling(window=200).mean()
    
    latest_close = df['Close'].iloc[-1]
    latest_sma = df['200SMA'].iloc[-1]
    diff_pct = ((latest_close - latest_sma) / latest_sma) * 100
    
    status = "🟢 Trend wzrostowy (Byk)" if latest_close >= latest_sma else "🔴 Korekta / Trend spadkowy (Niedźwiedź)"
    
    msg = (
        f"📊 <b>Raport QQQ (200 SMA)</b>\n\n"
        f"• <b>Cena zamknięcia:</b> ${latest_close:.2f}\n"
        f"• <b>200 SMA:</b> ${latest_sma:.2f}\n"
        f"• <b>Odchylenie:</b> {diff_pct:+.2f}%\n"
        f"• <b>Status:</b> {status}"
    )
    return msg

def wyslij_telegram(tekst):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": tekst, "parse_mode": "HTML"}
    response = requests.post(url, json=payload)
    response.raise_for_status()

if __name__ == "__main__":
    raport = analyze_qqq()
    wyslij_telegram(raport)
