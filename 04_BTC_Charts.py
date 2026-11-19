"""
04_BTC_Charts.py

Dwa wykresy BTC/USD (świece + wolumen + SMA 50) wysyłane na Telegram:
  - W1 (tygodniowy)
  - M1 (miesięczny)
Zakres wykresu: ostatnie 12 miesięcy. SMA 50 liczona na pełnej historii z Kraken,
dopiero potem wykres jest przycinany (inaczej SMA 50 na rocznym wycinku byłaby fikcją).

Źródło danych (darmowe, bez kluczy):
  - Kraken: świece W1 (do 720 tygodni = ok. 14 lat). Kraken nie ma interwału
    miesięcznego (max 15D), więc M1 jest składane z W1 (resample po miesiącu
    otwarcia tygodnia - granice miesięcy są przybliżone). Bieżący miesiąc jest
    niedomknięty i oznaczony w podpisie.

Ostatnia (niedomknięta) świeca tygodniowa jest pomijana - jak w 03_BTC_Signal.py.

Zmienne środowiskowe:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  TELEGRAM_THREAD_WYKRESY   - ID tematu #WYKRESY (93)

Uruchomienie lokalne bez wysyłki:  python 04_BTC_Charts.py --dry-run
"""

import os
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")

import requests
import pandas as pd
import mplfinance as mpf

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_WYKRESY")  # ID tematu #WYKRESY (93)

TZ = ZoneInfo("Europe/Warsaw")
NBSP = "\u00a0"
HTTP_TIMEOUT = 30
OUT_DIR = Path("tmp_charts")

KRAKEN_W1 = 10080
SMA_PERIOD = 50
LOOKBACK_MONTHS = 12
OHLC_COLS = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}


# ---------------------------------------------------------------- HTTP / dane

def http_get(url, params=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                             headers={"User-Agent": "btc-charts-bot"})
            if r.status_code >= 400:
                err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]!r}")
                if r.status_code < 500 and r.status_code != 429:
                    raise err            # bledu klienta nie ma sensu ponawiac
                last = err
                time.sleep(2 * (i + 1))
                continue
            return r.json()
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def fetch_kraken_weekly():
    data = http_get("https://api.kraken.com/0/public/OHLC",
                    {"pair": "XBTUSD", "interval": KRAKEN_W1})
    if data.get("error"):
        raise RuntimeError(data["error"])
    result = data["result"]
    key = next(k for k in result if k != "last")
    rows = result[key][:-1]  # ostatnia swieca jest niedomknieta
    df = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close",
                                     "vwap", "Volume", "count"])
    df.index = pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_localize(None)
    return df[list(OHLC_COLS)].astype(float).sort_index()


def to_monthly(weekly):
    return weekly.resample("MS").agg(OHLC_COLS).dropna()


# ---------------------------------------------------------------- wykres

def prepare(df):
    """SMA na pelnej historii, potem przyciecie do ostatnich 12 miesiecy."""
    df = df.copy()
    df["SMA"] = df["Close"].rolling(SMA_PERIOD).mean()
    start = df.index[-1] - pd.DateOffset(months=LOOKBACK_MONTHS)
    return df.loc[df.index > start]


def build_chart(df, label, path):
    sma = df["SMA"]
    addplots = [mpf.make_addplot(sma, color="#1f77b4", width=1.6)] if sma.notna().any() else []
    mpf.plot(
        df, type="candle", style="yahoo", volume=True, addplot=addplots,
        title=f"BTC/USD {label} - ostatnie {LOOKBACK_MONTHS} mies. (SMA {SMA_PERIOD})",
        ylabel="USD", ylabel_lower="Wolumen", figsize=(14, 8), tight_layout=True,
        savefig={"fname": str(path), "dpi": 150, "bbox_inches": "tight"},
    )


# ---------------------------------------------------------------- formatowanie

def usd(v):
    return "$" + f"{v:,.0f}".replace(",", NBSP)


def build_caption(label, df, today_iso):
    last = df.iloc[-1]
    sma = last["SMA"]
    if pd.notna(sma):
        dev = (last["Close"] / sma - 1) * 100
        icon = "🟢" if dev > 0 else "🔴"
        sma_txt = f"{icon} SMA {SMA_PERIOD}: {usd(sma)} (cena {dev:+.1f}%)"
    else:
        sma_txt = f"⚪️ SMA {SMA_PERIOD}: N/D - za mało świec"
    note = "\n<i>Bieżący miesiąc niedomknięty, miesiące złożone z W1 (przybliżenie).</i>" \
        if label == "M1" else ""
    return (f"<b>₿ BTC/USD - wykres {label}</b> {today_iso}\n"
            f"Cena: <b>{usd(last['Close'])}</b> | Świec: {len(df)}\n"
            f"{sma_txt}{note}")


# ---------------------------------------------------------------- telegram

def send_telegram_photo(path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    print(f"Telegram: temat = {TELEGRAM_THREAD_ID or 'BRAK (wiadomość trafi do General)'}")
    with open(path, "rb") as f:
        res = requests.post(url, data=payload, files={"photo": f}, timeout=HTTP_TIMEOUT)
    if not res.ok:
        print(f"Błąd Telegrama: {res.text}")
    res.raise_for_status()


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki na Telegram")
    args = parser.parse_args()

    weekly = fetch_kraken_weekly()
    if len(weekly) < SMA_PERIOD + 52:
        sys.exit("Za mało danych W1 z Kraken - przerywam.")

    today_iso = datetime.now(TZ).strftime("%d.%m.%Y")
    OUT_DIR.mkdir(exist_ok=True)

    charts = []
    for label, frame in (("W1", weekly), ("M1", to_monthly(weekly))):
        df = prepare(frame)
        path = OUT_DIR / f"BTC_{label}.png"
        build_chart(df, label, path)
        caption = build_caption(label, df, today_iso)
        print(caption)
        charts.append((path, caption))

    if args.dry_run:
        print(f"Dry-run: wykresy zapisane w {OUT_DIR}/, bez wysyłki.")
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        sys.exit("Brak TELEGRAM_TOKEN / TELEGRAM_CHAT_ID.")
    for path, caption in charts:
        send_telegram_photo(path, caption)


if __name__ == "__main__":
    main()
