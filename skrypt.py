import os
import requests
import yfinance as yf
import pandas as pd

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

GROUPS = {
    "COINBASE": [
        {"name": "MSFT Perp", "ticker": "MSFT"},
        {"name": "GOOGL Perp", "ticker": "GOOGL"},
        {"name": "QQQ Perp", "ticker": "NQ=F"}  # Użycie NQ=F zwraca wycenę kontraktu Futures / NASDAQ 100 (~700-800 USD)
    ],
    "WEEX": [
        {"name": "QQQUSDT", "ticker": "NQ=F"},
        {"name": "SPYUSDT", "ticker": "SPY"},
        {"name": "BTCUSDT", "ticker": "BTC-USD"}
    ]
}

def get_asset_data(asset_info):
    ticker_symbol = asset_info["ticker"]
    name = asset_info["name"]

    ticker = yf.Ticker(ticker_symbol)
    df = ticker.history(period="1y")

    if df.empty or len(df) < 200:
        return None

    df['200SMA'] = df['Close'].rolling(window=200).mean()

    latest_close = df['Close'].iloc[-1]
    latest_sma = df['200SMA'].iloc[-1]
    diff_pct = ((latest_close - latest_sma) / latest_sma) * 100

    close_7d = df['Close'].iloc[-6] if len(df) >= 6 else df['Close'].iloc[0]
    close_30d = df['Close'].iloc[-22] if len(df) >= 22 else df['Close'].iloc[0]

    change_7d = ((latest_close - close_7d) / close_7d) * 100
    change_30d = ((latest_close - close_30d) / close_30d) * 100

    status = "Wzrostowy (Bullish) 🟢" if latest_close >= latest_sma else "Spadkowy / Korekta (Bearish) 🔴"

    return {
        "name": name,
        "latest_close": latest_close,
        "latest_sma": latest_sma,
        "diff_pct": diff_pct,
        "status": status,
        "change_7d": change_7d,
        "change_30d": change_30d
    }

def format_colored_month(change_30d, max_pos, max_neg):
    """
    Formatowanie koloru tekstu dla wartości miesięcznej (M):
    - Wzrosty: Najlepszy = zielony, Pozostałe = żółty
    - Spadki: Największy minus = czerwony, Pozostałe = pomarańczowy
    """
    val_str = f"{change_30d:+.1f}%"
    
    if change_30d >= 0:
        if change_30d == max_pos and max_pos > 0:
            return f'🟢 <font color="#00FF00"><b>{val_str}</b></font>'
        else:
            return f'🟡 <font color="#FFFF00"><b>{val_str}</b></font>'
    else:
        if change_30d == max_neg and max_neg < 0:
            return f'🔴 <font color="#FF0000"><b>{val_str}</b></font>'
        else:
            return f'🟠 <font color="#FFA500"><b>{val_str}</b></font>'

def main():
    raport_parts = ["📊 <b>RAPORT SYGNAŁÓW I POTENCJAŁU WALORÓW</b>"]

    for group_name, assets in GROUPS.items():
        # Pobranie danych dla całej grupy
        parsed_assets = []
        for asset in assets:
            data = get_asset_data(asset)
            if data:
                parsed_assets.append(data)

        if not parsed_assets:
            continue

        # Wyznaczenie wartości ekstremalnych dla grupy (do kolorowania M)
        pos_changes = [a["change_30d"] for a in parsed_assets if a["change_30d"] >= 0]
        neg_changes = [a["change_30d"] for a in parsed_assets if a["change_30d"] < 0]

        max_pos = max(pos_changes) if pos_changes else None
        max_neg = min(neg_changes) if neg_changes else None

        # Pusta linijka przed nagłówkiem grupy
        raport_parts.append(f"\n🏛 <b>GRUPA {group_name}</b>\n")

        for a in parsed_assets:
            formatted_m = format_colored_month(a["change_30d"], max_pos, max_neg)
            
            msg = (
                f"📌 <b>{a['name']}</b>\n"
                f"• <b>Cena rynkowa (Mark Price):</b> ${a['latest_close']:,.2f}\n"
                f"• <b>Poziom 200 SMA (Stop Loss):</b> ${a['latest_sma']:,.2f}\n"
                f"• <b>Odchylenie od 200 SMA:</b> {a['diff_pct']:+.1f}%\n"
                f"• <b>Główny trend (D1/W1):</b> {a['status']}\n"
                f"• <b>Wzrost T / M:</b> {a['change_7d']:+.1f}% / {formatted_m}\n"
            )
            raport_parts.append(msg)

    pelny_raport = "\n".join(raport_parts)
    wyslij_telegram(pelny_raport)

def wyslij_telegram(tekst):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": tekst, "parse_mode": "HTML"}
    response = requests.post(url, json=payload)
    response.raise_for_status()

if __name__ == "__main__":
    main()
