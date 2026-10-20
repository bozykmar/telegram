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
        {"name": "QQQ Perp", "ticker": "QQQ"}  # Zmieniono z NQ=F na QQQ (wartość ~730 USD)
    ],
    "WEEX": [
        {"name": "QQQUSDT", "ticker": "QQQ"},   # Zmieniono z NQ=F na QQQ (wartość ~730 USD)
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

    # Wyliczenie 200 SMA na interwale dziennym
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

def format_colored_val(val, max_pos, max_neg):
    """
    Formatowanie wartości procentowych z emoji i pogrubieniem (zgodnie z Telegram HTML):
    - Wzrosty: Najlepszy = 🟢, Pozostałe = 🟡
    - Spadki: Największy minus = 🔴, Pozostałe = 🟠
    """
    val_str = f"{val:+.1f}%"
    
    if val >= 0:
        if val == max_pos and max_pos > 0:
            return f"🟢 <b>{val_str}</b>"
        else:
            return f"🟡 <b>{val_str}</b>"
    else:
        if val == max_neg and max_neg < 0:
            return f"🔴 <b>{val_str}</b>"
        else:
            return f"🟠 <b>{val_str}</b>"

def main():
    raport_parts = ["📊 <b>RAPORT SYGNAŁÓW I POTENCJAŁU WALORÓW</b>"]

    for group_name, assets in GROUPS.items():
        parsed_assets = []
        for asset in assets:
            data = get_asset_data(asset)
            if data:
                parsed_assets.append(data)

        if not parsed_assets:
            continue

        # Wartości ekstremalne dla Tygodnia (T)
        pos_t = [a["change_7d"] for a in parsed_assets if a["change_7d"] >= 0]
        neg_t = [a["change_7d"] for a in parsed_assets if a["change_7d"] < 0]
        max_pos_t = max(pos_t) if pos_t else None
        max_neg_t = min(neg_t) if neg_t else None

        # Wartości ekstremalne dla Miesiąca (M)
        pos_m = [a["change_30d"] for a in parsed_assets if a["change_30d"] >= 0]
        neg_m = [a["change_30d"] for a in parsed_assets if a["change_30d"] < 0]
        max_pos_m = max(pos_m) if pos_m else None
        max_neg_m = min(neg_m) if neg_m else None

        # Pusta linijka (odstęp) przed nagłówkiem grupy
        raport_parts.append(f"\n🏛 <b>GRUPA {group_name}</b>\n")

        for a in parsed_assets:
