"""
00_Walory.py

Raport sygnałów i potencjału walorów wysyłany na Telegram (2x dziennie: 09:15 i 20:15, czas polski).
Dla każdego waloru: cena, 200 SMA (D1), odchylenie od SMA, trend oraz zmiana tygodniowa (T)
i miesięczna (M). Zmiany liczone kalendarzowo (7 i 30 dni), więc akcje i BTC (handel 7 dni
w tygodniu) są porównywalne.

Źródło danych: Yahoo Finance (yfinance), bez kluczy API.

Konfiguracja (GitHub Actions variables, przekazane jako zmienne środowiskowe):
  TELEGRAM_TOKEN          - token bota
  TELEGRAM_CHAT_ID        - ID czatu / grupy
  TELEGRAM_THREAD_WALORY  - ID tematu w grupie (opcjonalnie)

Lokalnie bez wysyłki:  python 00_Walory.py --dry-run
Kod wyjścia: 0 = OK lub błędy częściowe (ostrzeżenie w logu), 1 = brak danych dla wszystkich walorów.
"""

import sys
import time
import html
import argparse

import pandas as pd
import yfinance as yf

from common import Telegram, fmt_usd

TG = Telegram("TELEGRAM_THREAD_WALORY")  # ID tematu Walory (31)

YF_ATTEMPTS = 3
SMA_PERIOD = 200
HISTORY_PERIOD = "2y"            # zapas ponad 200 sesji dla SMA
WEEK_DAYS, MONTH_DAYS = 7, 30

GROUPS = {
    "COINBASE": [
        {"name": "MSFT Perp", "ticker": "MSFT"},
        {"name": "GOOGL Perp", "ticker": "GOOGL"},
        {"name": "QQQ Perp", "ticker": "QQQ"},
    ],
    "WEEX": [
        {"name": "QQQUSDT", "ticker": "QQQ"},
        {"name": "SPYUSDT", "ticker": "SPY"},
        {"name": "BTCUSDT", "ticker": "BTC-USD"},
    ],
}


# ---------------------------------------------------------------- formatowanie

def format_colored_val(val, max_pos, max_neg):
    """
    Zmiana procentowa z emoji:
    - wzrosty: najlepszy w grupie = 🟢, pozostałe = 🟡
    - spadki: największy minus w grupie = 🔴, pozostałe = 🟠
    """
    val_str = f"{val:+.1f}%"
    if val >= 0:
        icon = "🟢" if (max_pos is not None and val == max_pos and max_pos > 0) else "🟡"
    else:
        icon = "🔴" if (max_neg is not None and val == max_neg and max_neg < 0) else "🟠"
    return f"{icon} {val_str}"


# ---------------------------------------------------------------- dane

def fetch_history(symbol):
    """Dzienna historia z ponawianiem (Yahoo bywa chwilowo niedostępne)."""
    last = None
    for attempt in range(YF_ATTEMPTS):
        try:
            df = yf.Ticker(symbol).history(period=HISTORY_PERIOD, auto_adjust=False)
            if df is not None and not df.empty:
                return df
            last = RuntimeError("pusta odpowiedź")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2 * (attempt + 1))
    raise last


def close_days_ago(close, days):
    """Ostatnie zamknięcie z dnia nie późniejszego niż (ostatnia data - days)."""
    target = close.index[-1] - pd.Timedelta(days=days)
    older = close[close.index <= target]
    return float(older.iloc[-1]) if not older.empty else float(close.iloc[0])


def get_asset_data(asset):
    try:
        df = fetch_history(asset["ticker"])
    except Exception as e:  # noqa: BLE001
        print(f"[BLAD] {asset['name']} ({asset['ticker']}): {e}")
        return None

    close = df["Close"].dropna()
    if len(close) < SMA_PERIOD:
        print(f"[BLAD] {asset['name']}: za mało danych ({len(close)} < {SMA_PERIOD})")
        return None

    latest_close = float(close.iloc[-1])
    latest_sma = float(close.rolling(SMA_PERIOD).mean().iloc[-1])
    close_7d = close_days_ago(close, WEEK_DAYS)
    close_30d = close_days_ago(close, MONTH_DAYS)

    return {
        "name": asset["name"],
        "latest_close": latest_close,
        "latest_sma": latest_sma,
        "diff_pct": (latest_close / latest_sma - 1) * 100,
        "bullish": latest_close >= latest_sma,
        "change_7d": (latest_close / close_7d - 1) * 100,
        "change_30d": (latest_close / close_30d - 1) * 100,
    }


# ---------------------------------------------------------------- komunikat

def extremes(values):
    pos = [v for v in values if v >= 0]
    neg = [v for v in values if v < 0]
    return (max(pos) if pos else None), (min(neg) if neg else None)


def build_report(groups_data):
    parts = ["📊 RAPORT SYGNAŁÓW I POTENCJAŁU WALORÓW"]
    for group_name, assets in groups_data.items():
        if not assets:
            continue
        max_pos_t, max_neg_t = extremes([a["change_7d"] for a in assets])
        max_pos_m, max_neg_m = extremes([a["change_30d"] for a in assets])

        parts.append(f"\n🏛 <b>GRUPA {html.escape(group_name)}</b>\n")
        for a in assets:
            status = "Wzrostowy (Bullish) 🟢" if a["bullish"] else "Spadkowy / Korekta (Bearish) 🔴"
            parts.append(
                f"📌 {html.escape(a['name'])}\n"
                f"• Cena rynkowa (Mark Price): {fmt_usd(a['latest_close'])}\n"
                f"• Poziom 200 SMA (Stop Loss): {fmt_usd(a['latest_sma'])}\n"
                f"• Odchylenie od 200 SMA: {a['diff_pct']:+.1f}%\n"
                f"• Główny trend (D1): {status}\n"
                f"• Wzrost T / M: {format_colored_val(a['change_7d'], max_pos_t, max_neg_t)} / "
                f"{format_colored_val(a['change_30d'], max_pos_m, max_neg_m)}\n"
            )
    return "\n".join(parts)


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki na Telegram")
    args = parser.parse_args()

    if not args.dry_run:
        TG.require()

    groups_data = {name: [d for d in (get_asset_data(a) for a in assets) if d]
                   for name, assets in GROUPS.items()}
    total = sum(len(v) for v in groups_data.values())
    expected = sum(len(v) for v in GROUPS.values())
    if total == 0:
        sys.exit("Brak danych dla wszystkich walorów - przerywam.")
    if total < expected:
        print(f"Ostrzeżenie: dane tylko dla {total}/{expected} walorów.")

    report = build_report(groups_data)
    print(report)
    if not args.dry_run:
        TG.send_message(report)


if __name__ == "__main__":
    main()
