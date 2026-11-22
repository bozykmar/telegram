"""
05_BTC_Strefy.py

Trzy komunikaty na Telegram, wysyłane jeden po drugim w KAŻDYM przebiegu (2x dziennie: 09:15 i 20:15,
czas polski), każdy jako OSOBNA wiadomość:
  1. BTC - strefy skalowania pozycji (progi cenowe, depozyt zabezpieczający, cena likwidacji),
  2. ETH - strefy skalowania pozycji (j.w.),
  3. BTC - sygnały wskaźnika TradingView "#1 Strefy BTC" (D1): wejścia LONG / SHORT, zielony i czerwony X,
     przesunięcia trailing stopa, zmiany fazy cyklu halvingowego oraz dzienny status.
Błąd jednego komunikatu nie blokuje pozostałych (run kończy się wtedy na czerwono).

Konfiguracja (GitHub Actions variables, przekazane jako zmienne środowiskowe):
  TELEGRAM_TOKEN           - token bota
  TELEGRAM_CHAT_ID         - ID czatu / grupy
  TELEGRAM_THREAD_STREFY   - ID tematu w grupie (opcjonalnie; bez niej wiadomości trafią do General)

Uruchomienie lokalne bez wysyłki i bez zapisu stanu:  python 05_BTC_Strefy.py --dry-run

--- 1-2. Strefy skalowania (dla każdej pozycji; tylko progi nie wyższe niż aktualny kurs) ---
a) Przy przejściu przez próg depozyt rośnie o TOPUP_SHARE niezrealizowanego zysku od poprzedniego
   progu:  M += TOPUP_SHARE x q x (cena_progu - poprzednia_cena).
b) Dokupienie dq to mniejsza z dwóch wartości:
   - limit dźwigni:        dq <= MAX_LEVERAGE x M / cena - q,
   - limit likwidacji:     dq <= (M - q x (avg - LIQ_RATIO x cena)) / ((1 - LIQ_RATIO) x cena).
c) Jeśli dq > 0, średnia cena wejścia avg i wielkość pozycji q są aktualizowane.
d) Cena likwidacji = avg - M / q.
Kurs: Binance (data-api.binance.vision, potem api.binance.com), zapas OKX. Binance blokuje część
adresów IP z USA (runnery GitHub), dlatego pierwszy jest publiczny mirror danych rynkowych.

--- 3. Sygnały wskaźnika ---
Skrypt odtwarza logikę wskaźnika na dziennych świecach BTC. Źródło: yfinance (BTC-USD od 2014 r.),
awaryjnie Kraken (ok. 720 świec D1 - stan pozycji może wtedy odbiegać od wykresu TradingView).
Wskaźnik "pamięta" wejścia, więc cała historia jest przeliczana przy każdym uruchomieniu. TradingView
używa innego źródła cen, więc pojedyncze sygnały mogą różnić się o jedną świecę. To nie jest porada
inwestycyjna. Komunikat 3 wysyłany jest w każdym przebiegu; sekcja NOWE SYGNAŁY pokazuje tylko zdarzenia
jeszcze niezgłoszone - lista zgłoszonych zdarzeń jest w 05_Strefy_History.json.
Ostatni halving (2028) jest szacunkowy - po prawdziwym halvingu zaktualizuj HALVINGS.
"""

import sys
import time
import argparse
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from functools import partial

import numpy as np
import pandas as pd

from common import NBSP, TZ, Telegram, fmt_usd, http_get, load_json, save_json

TG = Telegram("TELEGRAM_THREAD_STREFY")
USER_AGENT = "btc-strefy-bot"
SEND_PAUSE = 1.1          # przerwa między wiadomościami (limit Telegrama)

# ================================================================ 1-2. strefy skalowania

BINANCE_URLS = (
    "https://data-api.binance.vision/api/v3/ticker/price",
    "https://api.binance.com/api/v3/ticker/price",
)
OKX_URL = "https://www.okx.com/api/v5/market/ticker"

MAX_LEVERAGE = 5          # maksymalna dźwignia po dokupieniu
LIQ_RATIO = 0.60          # cena likwidacji nie wyżej niż 60% ceny progu
TOPUP_SHARE = 0.5         # jaka część niezrealizowanego zysku trafia do depozytu


@dataclass(frozen=True)
class Position:
    symbol: str           # para na giełdzie, np. BTCUSDT
    qty: float            # wielkość pozycji (w jednostkach monety)
    avg_price: float      # średnia cena wejścia
    margin: float         # depozyt zabezpieczający (USDT)
    start_price: float    # cena, od której liczone są progi


@dataclass(frozen=True)
class ZoneState:
    threshold: float | None   # najwyższy próg nie wyższy niż kurs (None = kurs poniżej pierwszego)
    margin: float
    liquidation: float


POSITIONS = {
    "BTC": Position("BTCUSDT", qty=0.9124, avg_price=60752.8, margin=18476.98, start_price=60752.8),
    "ETH": Position("ETHUSDT", qty=31.87, avg_price=1705.53, margin=18118.43, start_price=1705.53),
}

THRESHOLDS = {
    "BTC": list(range(70000, 200001, 10000)),
    "ETH": [1965, 2246, 2527, 2807, 3088, 3369, 3650, 3930, 4211, 4492,
            4772, 5053, 5334, 5615],
}


def fetch_price(symbol):
    """Aktualny kurs: Binance (dwa adresy), w razie błędu OKX."""
    for url in BINANCE_URLS:
        try:
            return float(http_get(url, {"symbol": symbol}, user_agent=USER_AGENT)["price"])
        except Exception as e:  # noqa: BLE001
            print(f"[BLAD] {url} {symbol}: {e}")
    inst_id = symbol.replace("USDT", "-USDT")
    data = http_get(OKX_URL, {"instId": inst_id}, user_agent=USER_AGENT)
    return float(data["data"][0]["last"])


def scale_step(qty, avg_price, margin, last_price, price):
    """Jeden próg: doładowanie depozytu i dokupienie. Zwraca (qty, avg_price, margin)."""
    margin += TOPUP_SHARE * qty * (price - last_price)
    max_by_leverage = MAX_LEVERAGE * margin / price - qty
    max_by_liquidation = ((margin - qty * (avg_price - LIQ_RATIO * price))
                          / ((1 - LIQ_RATIO) * price))
    added = min(max_by_leverage, max_by_liquidation)
    if added > 0:
        avg_price = (avg_price * qty + price * added) / (qty + added)
        qty += added
    return qty, avg_price, margin


def simulate(position, thresholds, price):
    """Przechodzi po progach nie wyższych niż kurs i zwraca stan po ostatnim z nich."""
    qty, avg_price, margin = position.qty, position.avg_price, position.margin
    last_price = position.start_price
    reached = None
    for threshold in thresholds:
        if threshold > price:
            break
        qty, avg_price, margin = scale_step(qty, avg_price, margin, last_price, threshold)
        last_price = threshold
        reached = threshold
    return ZoneState(reached, margin, avg_price - margin / qty)


def number(value):
    return f"{value:,.0f}".replace(",", NBSP)


def usd(value):
    return fmt_usd(value, 0)


def pct(value, decimals=1):
    return f"{value:+.{decimals}f}%"


def zone_message(name, today):
    """Komunikat 1-2: strefy skalowania jednej pozycji. Zwraca (tekst, None)."""
    position = POSITIONS[name]
    price = fetch_price(position.symbol)
    state = simulate(position, THRESHOLDS[name], price)
    if state.threshold is None:
        threshold_line = "brak (kurs poniżej pierwszego progu)"
        margin_label = " (pozycja startowa)"
    else:
        threshold_line = f"<b>{number(state.threshold)}</b>"
        margin_label = ""
    text = (
        f"<b>📐 {name} - strefy skalowania</b> - {today}\n"
        f"Kurs: <b>{fmt_usd(price, 2)}</b>\n"
        f"Najniższa cena poniżej kursu: {threshold_line}\n"
        f"Depozyt zabezp.: <b>{number(state.margin)} USDT</b>{margin_label}\n"
        f"Likwidacja: <b>{usd(state.liquidation)}</b>\n"
    )
    return text, None


# ================================================================ 3. sygnały wskaźnika

STATE_FILE = "05_Strefy_History.json"
NOTIFIED_KEEP = 80
EVENT_WINDOW = 3          # zdarzenia z ostatnich N świec (zamkniętych + dzisiejszej)
MIN_CANDLES = 400         # minimum świec D1 do przeliczenia
FULL_HISTORY = 1500       # poniżej tej liczby świec yfinance uznajemy za niepełne

MIN_BARS = 2              # min. liczba tygodni konsolidacji z rzędu
RSI_LOW, RSI_HIGH = 35, 65
SQZ_MULT = 1.0            # mnożnik zwężenia Bollingera
USE_VOL = False           # wymagaj spadku wolumenu
RESET_BREAK = False       # kasuj kanał po wybiciu
ATR_LEN = 14
SMA_PERIOD = 200
NEED_SMA = True           # LONG tylko nad SMA, SHORT tylko pod SMA

PEAK_DAY = 535            # dzień szczytu cyklu po halvingu
BOTTOM_DAY = 915          # dzień dołka cyklu po halvingu
SHORT_ENTRY_LEN = 150     # okno wejść SHORT po szczycie (dni)
PEAK_GRACE = 30           # wymuś zamknięcie LONG X dni po szczycie
COVER_MARGIN = 20         # wymuś zamknięcie SHORT X dni przed dołkiem
FORCE_EXIT = True

LEVERAGE = 3.0
LIQ_SAFETY = 0.6
MMR_PCT = 0.5
WIDE_MULT = 4.5
TIGHT_MULT = 1.5
RAMP_DAYS = 120
USE_SMA_STOP = True
SMA_BUF = 5.0
USE_CH_STOP = True
CH_BUF = 3.0
SMA_EXIT = False
MOVE_STEP = 1.0           # minimalne przesunięcie SL do komunikatu (%)

MAX_LOSS_PCT = 100.0 / LEVERAGE * LIQ_SAFETY

HALVINGS = [date(2012, 11, 28), date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 20),
            date(2028, 4, 20)]   # ostatni - szacunkowy
CYCLE_DAYS = 1461         # długość cyklu po ostatnim znanym halvingu


# ---------------------------------------------------------------- dane

def normalize_daily(df):
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df = df.copy()
    df.index = idx.normalize()
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch_daily_yfinance():
    import yfinance as yf
    df = yf.Ticker("BTC-USD").history(period="max", interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance zwrócił puste dane")
    return normalize_daily(df)


def fetch_daily_kraken():
    data = http_get("https://api.kraken.com/0/public/OHLC", {"pair": "XBTUSD", "interval": 1440},
                    user_agent=USER_AGENT)
    if data.get("error"):
        raise RuntimeError(data["error"])
    result = data["result"]
    rows = result[next(k for k in result if k != "last")]
    df = pd.DataFrame(
        {"Open": [float(r[1]) for r in rows], "High": [float(r[2]) for r in rows],
         "Low": [float(r[3]) for r in rows], "Close": [float(r[4]) for r in rows],
         "Volume": [float(r[6]) for r in rows]},
        index=pd.to_datetime([int(r[0]) for r in rows], unit="s"))
    return normalize_daily(df)


def load_daily():
    try:
        df = fetch_daily_yfinance()
        if len(df) >= FULL_HISTORY:
            return df, "yfinance"
        print(f"yfinance: za mało danych ({len(df)} świec) - próbuję Kraken")
    except Exception as e:  # noqa: BLE001
        print(f"[BLAD] yfinance: {e}")
    df = fetch_daily_kraken()
    print("UWAGA: dane z Kraken (ok. 720 świec) - stan pozycji może różnić się od TradingView")
    return df, "Kraken (ograniczona historia)"


# ---------------------------------------------------------------- wskaźniki

def rma(values, n):
    """Odpowiednik ta.rma (start = SMA pierwszych n wartości)."""
    out = np.full(len(values), np.nan)
    acc, cnt, prev = 0.0, 0, None
    for i, v in enumerate(values):
        if np.isnan(v):
            continue
        if prev is None:
            acc += v
            cnt += 1
            if cnt == n:
                prev = acc / n
                out[i] = prev
        else:
            prev = (prev * (n - 1) + v) / n
            out[i] = prev
    return out


def rsi_series(close, n=14):
    ch = np.diff(close, prepend=np.nan)
    up = np.where(np.isnan(ch), np.nan, np.maximum(ch, 0.0))
    dn = np.where(np.isnan(ch), np.nan, np.maximum(-ch, 0.0))
    avg_up, avg_dn = rma(up, n), rma(dn, n)
    out = np.full(len(close), np.nan)
    ok = ~np.isnan(avg_up) & ~np.isnan(avg_dn)
    with np.errstate(divide="ignore", invalid="ignore"):
        vals = 100 - 100 / (1 + avg_up / avg_dn)
    out[ok] = vals[ok]
    out[ok & (avg_dn == 0)] = 100.0
    return out


def atr_series(high, low, close, n=14):
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    tr[0] = high[0] - low[0]
    return rma(tr, n)


def weekly_channel(df):
    """
    Kanał konsolidacji W1 (odpowiednik calc_htf). Zwraca DataFrame indeksowany końcem tygodnia
    (niedziela) z kolumnami up, lo, cons, atr - wartości z poprzedniej, zamkniętej świecy tygodniowej.
    """
    wk = df.resample("W-SUN", closed="right", label="right").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Close"])
    c, h, l, v = (wk[k].to_numpy(float) for k in ("Close", "High", "Low", "Volume"))
    n = len(wk)

    close = pd.Series(c)
    basis = close.rolling(20, min_periods=20).mean().to_numpy()
    dev = close.rolling(20, min_periods=20).std(ddof=0).to_numpy()
    bbw = 4.0 * dev / basis
    avg_bbw = pd.Series(bbw).rolling(20, min_periods=20).mean().to_numpy()
    rsi = rsi_series(c, 14)

    with np.errstate(invalid="ignore"):
        cond = (rsi >= RSI_LOW) & (rsi <= RSI_HIGH) & (bbw < avg_bbw * SQZ_MULT)
        if USE_VOL:
            cond &= v < pd.Series(v).rolling(20, min_periods=20).mean().to_numpy()

    up = np.full(n, np.nan)
    lo = np.full(n, np.nan)
    cons = np.zeros(n)
    cnt = 0
    up_b = lo_b = np.nan
    for k in range(n):
        cnt = cnt + 1 if cond[k] else 0
        cons[k] = float(cnt >= MIN_BARS)
        length = max(cnt, 2)
        if cons[k] and k - length + 1 >= 0:
            up_b = h[k - length + 1:k + 1].max()
            lo_b = l[k - length + 1:k + 1].min()
        elif RESET_BREAK and k > 0 and (
                c[k] > (up[k - 1] if not np.isnan(up[k - 1]) else 1e10)
                or c[k] < (lo[k - 1] if not np.isnan(lo[k - 1]) else -1e10)):
            up_b = lo_b = np.nan
        up[k], lo[k] = up_b, lo_b

    channel = pd.DataFrame({"up": up, "lo": lo, "cons": cons,
                            "atr": atr_series(h, l, c, ATR_LEN)}, index=wk.index)
    return channel.shift(1)


# ---------------------------------------------------------------- cykl halvingowy

def cycle_info(d):
    last = max((h for h in HALVINGS if h <= d), default=None)
    if last is None:
        return None
    later = [h for h in HALVINGS if h > d]
    nxt = later[0] if later else HALVINGS[-1] + timedelta(days=CYCLE_DAYS)
    dd = (d - last).days
    long_zone = dd < PEAK_DAY or dd >= BOTTOM_DAY
    short_zone = PEAK_DAY <= dd < PEAK_DAY + SHORT_ENTRY_LEN
    if long_zone:
        phase = "Hossa (LONG)" if dd < PEAK_DAY else "Akumulacja (LONG)"
    elif short_zone:
        phase = "Bessa (okno SHORT)"
    else:
        phase = "Bessa (bez nowych wejść)"
    if dd < PEAK_DAY:
        rem_peak = PEAK_DAY - dd
    elif dd >= BOTTOM_DAY:
        rem_peak = (nxt - d).days + PEAK_DAY
    else:
        rem_peak = 0.0
    rem_bottom = BOTTOM_DAY - dd if dd < BOTTOM_DAY else 0.0

    def multiplier(rem):
        tightness = 0.0 if rem >= RAMP_DAYS else (1.0 if rem <= 0 else 1.0 - rem / RAMP_DAYS)
        return WIDE_MULT + (TIGHT_MULT - WIDE_MULT) * tightness

    return {
        "dd": dd, "phase": phase, "long_zone": long_zone, "short_zone": short_zone,
        "rem_peak": rem_peak, "rem_bottom": rem_bottom,
        "mult_long": multiplier(rem_peak), "mult_short": multiplier(rem_bottom),
        "long_force": FORCE_EXIT and PEAK_DAY + PEAK_GRACE <= dd < BOTTOM_DAY,
        "short_force": FORCE_EXIT and (dd >= BOTTOM_DAY - COVER_MARGIN or dd < PEAK_DAY),
    }


# ---------------------------------------------------------------- replay logiki wskaźnika

def replay(df):
    """Przelicza całą historię. Zwraca (stan ostatniej świecy, zdarzenia z ostatnich EVENT_WINDOW świec)."""
    n = len(df)
    dates = list(df.index.date)
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))

    week_end = df.index + pd.to_timedelta(6 - df.index.dayofweek, unit="D")
    channel = weekly_channel(df).reindex(week_end)
    up_d, lo_d, atr_d = (channel[k].to_numpy() for k in ("up", "lo", "atr"))
    cons_d = channel["cons"].to_numpy() == 1.0

    with np.errstate(invalid="ignore"):
        cross_up = np.zeros(n, dtype=bool)
        cross_dn = np.zeros(n, dtype=bool)
        cross_up[1:] = (c[1:] > up_d[1:]) & (c[:-1] <= up_d[:-1])
        cross_dn[1:] = (c[1:] < lo_d[1:]) & (c[:-1] >= lo_d[:-1])

    sma = pd.Series(c).rolling(SMA_PERIOD, min_periods=SMA_PERIOD).mean().shift(1).to_numpy()

    pos = 0
    stop = ext = entry = last_alert = exit_px = np.nan
    long_signal_prev = short_signal_prev = False
    events = []
    last = {}
    prev_phase = None

    for i in range(n):
        d = dates[i]
        ci = cycle_info(d)
        if ci is None:
            long_signal_prev = short_signal_prev = False
            continue
        has_sma = not np.isnan(sma[i])
        is_up = has_sma and c[i] > sma[i]
        is_down = has_sma and c[i] < sma[i]
        atr_use = atr_d[i] if not np.isnan(atr_d[i]) else c[i] * 0.06

        long_signal = pos == 0 and ci["long_zone"] and (not NEED_SMA or is_up) and cross_up[i]
        short_signal = pos == 0 and ci["short_zone"] and (not NEED_SMA or is_down) and cross_dn[i]

        entered = exited = None
        reason = ""
        if pos == 1:
            hit = l[i] < stop
            if hit or (SMA_EXIT and is_down) or ci["long_force"]:
                exited, pos = "LONG", 0
                reason = "stop" if hit else ("zmiana trendu (SMA)" if (SMA_EXIT and is_down) else "koniec fazy cyklu")
                exit_px = stop if hit else c[i]
            else:
                ext = max(ext, h[i])
                trail = ext - ci["mult_long"] * atr_use
                floor = (sma[i] if has_sma else 0.0) * (1 - SMA_BUF / 100) if USE_SMA_STOP else 0.0
                stop = max(stop, trail, floor)
        elif pos == -1:
            hit = h[i] > stop
            if hit or (SMA_EXIT and is_up) or ci["short_force"]:
                exited, pos = "SHORT", 0
                reason = "stop" if hit else ("zmiana trendu (SMA)" if (SMA_EXIT and is_up) else "koniec fazy cyklu")
                exit_px = stop if hit else c[i]
            else:
                ext = min(ext, l[i])
                trail = ext + ci["mult_short"] * atr_use
                ceiling = (sma[i] if has_sma else 1e12) * (1 + SMA_BUF / 100) if USE_SMA_STOP else 1e12
                stop = min(stop, trail, ceiling)
        elif long_signal_prev:
            entered, pos, entry, ext = "LONG", 1, o[i], h[i]
            fallback = o[i] - ci["mult_long"] * atr_use
            channel_sl = lo_d[i - 1] * (1 - CH_BUF / 100) if i > 0 else np.nan
            start = channel_sl if (USE_CH_STOP and not np.isnan(channel_sl) and channel_sl < o[i]) else fallback
            stop = max(start, o[i] * (1 - MAX_LOSS_PCT / 100))
            last_alert = stop
        elif short_signal_prev:
            entered, pos, entry, ext = "SHORT", -1, o[i], l[i]
            fallback = o[i] + ci["mult_short"] * atr_use
            channel_sl = up_d[i - 1] * (1 + CH_BUF / 100) if i > 0 else np.nan
            start = channel_sl if (USE_CH_STOP and not np.isnan(channel_sl) and channel_sl > o[i]) else fallback
            stop = min(start, o[i] * (1 + MAX_LOSS_PCT / 100))
            last_alert = stop

        recent = i >= n - EVENT_WINDOW
        if recent:
            iso = d.isoformat()
            if entered:
                events.append({"type": f"ENTER_{entered}", "date": iso, "price": float(o[i]), "stop": float(stop)})
            if exited:
                events.append({"type": f"EXIT_{exited}", "date": iso, "price": float(exit_px), "reason": reason})
            if prev_phase is not None and ci["phase"] != prev_phase:
                events.append({"type": "PHASE", "date": iso, "phase": ci["phase"]})
        if pos != 0 and not entered and not np.isnan(stop) and not np.isnan(last_alert):
            if abs(stop - last_alert) / last_alert * 100 >= MOVE_STEP:
                if recent:
                    events.append({"type": "MOVE", "date": d.isoformat(),
                                   "side": "LONG" if pos == 1 else "SHORT", "stop": float(stop)})
                last_alert = stop

        prev_phase = ci["phase"]
        long_signal_prev, short_signal_prev = long_signal, short_signal
        last = {"ci": ci, "pos": pos, "stop": stop, "entry": entry, "sma": sma[i], "is_up": is_up,
                "up": up_d[i], "lo": lo_d[i], "cons": bool(cons_d[i])}
    return last, events


# ---------------------------------------------------------------- komunikat sygnałów

def fmt_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


def event_text(e):
    t, day = e["type"], fmt_date(e["date"])
    if t in ("ENTER_LONG", "ENTER_SHORT"):
        icon, side = ("🟢", "LONG") if t == "ENTER_LONG" else ("🔴", "SHORT")
        return (f"{icon} <b>WEJŚCIE {side}</b> - otwarcie świecy {day}, cena {usd(e['price'])}, "
                f"SL startowy {usd(e['stop'])}")
    if t == "EXIT_LONG":
        return f"🟩 <b>ZIELONY X - zamknij LONG</b> ({e['reason']}), ok. {usd(e['price'])} [{day}]"
    if t == "EXIT_SHORT":
        return f"🟥 <b>CZERWONY X - zamknij SHORT</b> ({e['reason']}), ok. {usd(e['price'])} [{day}]"
    if t == "MOVE":
        return f"📐 <b>Przesuń SL {e['side']}</b> na {usd(e['stop'])} [{day}]"
    if t == "PHASE":
        return f"🔄 <b>Zmiana fazy cyklu:</b> {e['phase']} [{day}]"
    return t


def position_lines(last, spot):
    """Sekcja otwartej pozycji: SL, mnożnik ATR, likwidacja, maks. bezpieczna dźwignia."""
    ci, pos = last["ci"], last["pos"]
    long_side = pos == 1
    stop, entry = last["stop"], last["entry"]
    liq = entry * (1 - 1 / LEVERAGE + MMR_PCT / 100) if long_side else entry * (1 + 1 / LEVERAGE - MMR_PCT / 100)
    sl_to_entry = abs(entry - stop) / entry * 100
    max_lev = 100 / (sl_to_entry * 1.5) if sl_to_entry > 0 else None
    rem = ci["rem_peak"] if long_side else ci["rem_bottom"]
    lines = [
        f"{'🟢' if long_side else '🔴'} <b>Pozycja {'LONG' if long_side else 'SHORT'}</b>: wejście {usd(entry)}",
        f"🛡 <b>Ustaw SL na</b>: {usd(stop)} (odległość od ceny {abs(spot - stop) / spot * 100:.1f}%)",
        f"⚙️ Mnożnik ATR {ci['mult_long'] if long_side else ci['mult_short']:.1f}x "
        f"({rem:.0f} dni do {'szczytu' if long_side else 'dołka'})",
        f"💀 Likwidacja x{LEVERAGE:g}: ok. {usd(liq)}"
        + (f" | maks. bezpieczna dźwignia dla tego SL: {max_lev:.1f}x" if max_lev is not None else ""),
    ]
    if max_lev is not None and max_lev < LEVERAGE:
        lines.append(f"⚠️ Przy dźwigni {LEVERAGE:g}x stop jest za daleko - ryzyko likwidacji przed SL.")
    return lines


def waiting_line(last, spot):
    """Na co czeka wskaźnik, gdy nie ma pozycji."""
    ci, up, lo, is_up = last["ci"], last["up"], last["lo"], last["is_up"]
    if ci["long_zone"] and is_up and not np.isnan(up):
        return f"👀 Czekam na LONG: zamknięcie dzienne powyżej oporu {usd(up)} ({pct((up / spot - 1) * 100)})."
    if ci["long_zone"] and not is_up:
        return "👀 Faza pozwala na LONG, ale cena jest pod SMA 200 - czekam na jej odzyskanie."
    if ci["short_zone"] and not is_up and not np.isnan(lo):
        return f"👀 Czekam na SHORT: zamknięcie dzienne poniżej wsparcia {usd(lo)} ({pct((lo / spot - 1) * 100)})."
    if ci["short_zone"] and is_up:
        return "👀 Faza pozwala na SHORT, ale cena jest nad SMA 200 - czekam na jej utratę."
    return "👀 Faza cyklu nie dopuszcza nowych wejść."


def signal_text(today, spot, last, new_events, source, partial):
    ci, sma, up, lo = last["ci"], last["sma"], last["up"], last["lo"]
    lines = [f"<b>₿ BTC - Strefy konsolidacji</b> {today.strftime('%d.%m.%Y')}",
             f"Cena: <b>{usd(spot)}</b> | wykres D1, kanały W1 | dane: {source}"]
    if partial:
        lines.append("<i>Stan uwzględnia dzisiejszą, jeszcze otwartą świecę dzienną.</i>")
    lines.append("")
    if new_events:
        lines += ["<b>NOWE SYGNAŁY:</b>"] + [event_text(e) for e in new_events] + [""]

    zone = "tylko LONG" if ci["long_zone"] else "tylko SHORT" if ci["short_zone"] else "bez nowych wejść"
    lines += ["<b>Status:</b>",
              f"🔄 <b>Faza cyklu</b>: {ci['phase']} ({zone}), {ci['dd']} dni od halvingu"]
    if not np.isnan(sma):
        lines.append(f"{'🟢' if last['is_up'] else '🔴'} <b>Trend SMA 200</b>: "
                     f"{'wzrostowy' if last['is_up'] else 'spadkowy'}: cena {pct((spot / sma - 1) * 100)} vs {usd(sma)}")
    if not np.isnan(up) and not np.isnan(lo):
        lines.append(f"📏 <b>Kanał W1</b>: opór {usd(up)} ({pct((up / spot - 1) * 100)}), "
                     f"wsparcie {usd(lo)} ({pct((lo / spot - 1) * 100)})"
                     f"{' - konsolidacja aktywna' if last['cons'] else ''}")
    else:
        lines.append("📏 <b>Kanał W1</b>: brak aktywnego kanału")

    if last["pos"] != 0:
        lines += position_lines(last, spot)
    else:
        lines += ["⚪️ <b>Pozycja</b>: brak", waiting_line(last, spot)]

    lines += ["", "<i>Odtworzenie wskaźnika TradingView na danych yfinance - sygnały mogą różnić się od wykresu. "
                  "To nie jest porada inwestycyjna.</i>"]
    return "\n".join(lines)


def signal_message():
    """Komunikat 3 (zawsze wysyłany). Zwraca (tekst, zapis_stanu)."""
    df, source = load_daily()
    if len(df) < MIN_CANDLES:
        raise RuntimeError("za mało danych dziennych")

    today = datetime.now(timezone.utc).date()
    spot = float(df["Close"].iloc[-1])
    last, events = replay(df)
    if not last:
        raise RuntimeError("replay nie zwrócił stanu")
    print(f"Stan: poz={last['pos']}, SL={last['stop']}, faza={last['ci']['phase']}, zdarzeń={len(events)}")

    state = load_json(STATE_FILE, {})
    notified = list(state.get("notified", []))
    keyed = [(f"{e['type']}|{e['date']}|{e.get('side', '')}", e) for e in events]
    new_events = [e for key, e in keyed if key not in notified]
    text = signal_text(today, spot, last, new_events, source, df.index[-1].date() == today)

    def save_state():
        state["notified"] = (notified + [key for key, _ in keyed])[-NOTIFIED_KEEP:]
        save_json(STATE_FILE, state)

    return text, save_state


# ================================================================ main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki i bez zapisu stanu")
    args = parser.parse_args()

    if not args.dry_run:
        TG.require()

    today_pl = datetime.now(TZ).strftime("%d.%m.%Y")
    tasks = [(f"{name} strefy skalowania", partial(zone_message, name, today_pl)) for name in POSITIONS]
    tasks.append(("sygnały wskaźnika", signal_message))

    failed = 0
    for label, build in tasks:
        try:
            text, on_sent = build()
            print(text)
            if not args.dry_run:
                TG.send_message(text)
                if on_sent:
                    on_sent()
                time.sleep(SEND_PAUSE)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[BLAD] {label}: {e}")
    if failed:
        sys.exit(f"Nie udało się wysłać {failed} z {len(tasks)} komunikatów.")


if __name__ == "__main__":
    main()
