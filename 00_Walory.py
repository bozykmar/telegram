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

import os
import sys
import time
import html
import argparse

import requests
import pandas as pd
import yfinance as yf


def env(name):
    return (os.environ.get(name) or "").strip() or None


def env_int(name):
    value = env(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        print(f"Ignoruję {name}: '{value}' nie jest liczbą całkowitą.")
        return None


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = env_int("TELEGRAM_THREAD_WALORY")  # ID tematu Walory (31)

NBSP = "\u00a0"
HTTP_TIMEOUT = 30
TELEGRAM_MAX_LEN = 4000          # limit Telegrama to 4096 znaków; zapas na bezpieczeństwo
TELEGRAM_ATTEMPTS = 3
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

def usd(value):
    """$1 234.56 (twarda spacja jako separator tysięcy - kwota nie łamie się w nowej linii)."""
    return "$" + f"{value:,.2f}".replace(",", NBSP)


def format_colored_val(val, max_pos, max_neg):
    """
    Zmiana procentowa z emoji i pogrubieniem:
    - wzrosty: najlepszy w grupie = 🟢, pozostałe = 🟡
    - spadki: największy minus w grupie = 🔴, pozostałe = 🟠
    """
    val_str = f"{val:+.1f}%"
    if val >= 0:
        icon = "🟢" if (max_pos is not None and val == max_pos and max_pos > 0) else "🟡"
    else:
        icon = "🔴" if (max_neg is not None and val == max_neg and max_neg < 0) else "🟠"
    return f"{icon} <b>{val_str}</b>"


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
    parts = ["📊 <b>RAPORT SYGNAŁÓW I POTENCJAŁU WALORÓW</b>"]
    for group_name, assets in groups_data.items():
        if not assets:
            continue
        max_pos_t, max_neg_t = extremes([a["change_7d"] for a in assets])
        max_pos_m, max_neg_m = extremes([a["change_30d"] for a in assets])

        parts.append(f"\n🏛 <b>GRUPA {html.escape(group_name)}</b>\n")
        for a in assets:
            status = "Wzrostowy (Bullish) 🟢" if a["bullish"] else "Spadkowy / Korekta (Bearish) 🔴"
            parts.append(
                f"📌 <b>{html.escape(a['name'])}</b>\n"
                f"• <b>Cena rynkowa (Mark Price):</b> {usd(a['latest_close'])}\n"
                f"• <b>Poziom 200 SMA (Stop Loss):</b> {usd(a['latest_sma'])}\n"
                f"• <b>Odchylenie od 200 SMA:</b> {a['diff_pct']:+.1f}%\n"
                f"• <b>Główny trend (D1):</b> {status}\n"
                f"• <b>Wzrost T / M:</b> {format_colored_val(a['change_7d'], max_pos_t, max_neg_t)} / "
                f"{format_colored_val(a['change_30d'], max_pos_m, max_neg_m)}\n"
            )
    return "\n".join(parts)


# ---------------------------------------------------------------- Telegram

def split_message(message, limit=TELEGRAM_MAX_LEN):
    """Dzieli tekst na części po liniach (tagi HTML w liniach są zamknięte)."""
    if len(message) <= limit:
        return [message]
    chunks, current = [], ""
    for line in message.splitlines(keepends=True):
        while len(line) > limit:  # awaryjnie: pojedyncza zbyt długa linia
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


def send_telegram_chunk(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if TELEGRAM_THREAD_ID is not None:
        payload["message_thread_id"] = TELEGRAM_THREAD_ID

    last_error = None
    for attempt in range(TELEGRAM_ATTEMPTS):
        try:
            res = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_error = e
            time.sleep(2 * (attempt + 1))
            continue

        if res.status_code == 429:  # limit Telegrama - odczekaj zalecany czas
            try:
                wait = int(res.json().get("parameters", {}).get("retry_after", 5))
            except Exception:  # noqa: BLE001
                wait = 5
            last_error = requests.HTTPError(f"429 Too Many Requests: {res.text}")
            time.sleep(wait + 1)
            continue

        if not res.ok:
            print(f"Błąd Telegrama: {res.text}")
        res.raise_for_status()
        return

    raise last_error or RuntimeError("Nie udało się wysłać wiadomości na Telegram")


def send_telegram_message(message):
    chunks = split_message(message)
    for i, chunk in enumerate(chunks):
        send_telegram_chunk(chunk)
        if i < len(chunks) - 1:
            time.sleep(1.1)


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki na Telegram")
    args = parser.parse_args()

    if not args.dry_run and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Brak TELEGRAM_TOKEN lub TELEGRAM_CHAT_ID w zmiennych środowiskowych.")

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
        send_telegram_message(report)


if __name__ == "__main__":
    main()
