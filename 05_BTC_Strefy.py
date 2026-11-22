"""
05_BTC_Strefy.py

Sygnały ze wskaźnika TradingView "#1 Strefy BTC" (interwał D1) wysyłane jako komunikat na Telegram.
Skrypt odtwarza logikę wskaźnika na dziennych świecach BTC i zgłasza:
  - wejścia LONG / SHORT (wybicie kanału konsolidacji W1 zgodne z SMA 200 i fazą cyklu),
  - ZIELONY X (zamknij LONG) i CZERWONY X (zamknij SHORT),
  - przesunięcia trailing stop lossa (co najmniej 1% od ostatniego komunikatu),
  - zmiany fazy cyklu halvingowego,
  - dzienny status: faza cyklu, trend SMA, kanał (opór / wsparcie), pozycja, SL, cena likwidacji.

Źródło danych: yfinance (BTC-USD, historia od 2014 r.). Gdy yfinance nie odpowie, awaryjnie Kraken
(tylko ok. 720 świec D1 - stan pozycji może wtedy odbiegać od wykresu TradingView).

Ważne: stan pozycji jest zależny od historii (wskaźnik "pamięta" wejścia), więc skrypt przelicza całą
historię przy każdym uruchomieniu. TradingView korzysta z innego źródła cen (np. Bitstamp/Coinbase),
dlatego pojedyncze sygnały mogą się różnić o jedną świecę lub kilka dolarów. Wyniki NIE są poradą inwestycyjną.

Stan (lista wysłanych zdarzeń, data ostatniego podsumowania) zapisuje się w 05_Strefy_History.json -
workflow powinien go zatwierdzać do repozytorium.

Wspólne narzędzia (HTTP, Telegram, strefa czasowa) pochodzą z common.py.

Uruchomienie lokalne bez wysyłki:  python 05_BTC_Strefy.py --dry-run
Wymuszenie wysyłki:                python 05_BTC_Strefy.py --force
"""

import sys
import argparse
from datetime import datetime, date, timedelta, timezone

import numpy as np
import pandas as pd

from common import NBSP, TZ, Telegram, http_get, load_json, save_json

TG = Telegram("TELEGRAM_THREAD_STREFY")  # nowy temat Telegrama - dodaj zmienną TELEGRAM_THREAD_STREFY

STATE_FILE = "05_Strefy_History.json"
NOTIFIED_KEEP = 80
EVENT_WINDOW = 3          # zdarzenia z ostatnich N świec (zamkniętych + dzisiejszej)

# ---------------------------------------------------------------- parametry (jak w TradingView)

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
            date(2028, 4, 20)]   # ostatni - szacunkowy; po prawdziwym halvingu zaktualizuj


# ---------------------------------------------------------------- dane

def _normalize(df):
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
    return _normalize(df)


def fetch_daily_kraken():
    data = http_get("https://api.kraken.com/0/public/OHLC", {"pair": "XBTUSD", "interval": 1440},
                    user_agent="btc-strefy-bot")
    if data.get("error"):
        raise RuntimeError(data["error"])
    result = data["result"]
    key = next(k for k in result if k != "last")
    rows = result[key]
    df = pd.DataFrame({
        "Open": [float(r[1]) for r in rows], "High": [float(r[2]) for r in rows],
        "Low": [float(r[3]) for r in rows], "Close": [float(r[4]) for r in rows],
        "Volume": [float(r[6]) for r in rows],
    }, index=pd.to_datetime([int(r[0]) for r in rows], unit="s"))
    return _normalize(df)


def load_daily():
    try:
        df = fetch_daily_yfinance()
        if len(df) >= 1500:
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
    ru, rd = rma(up, n), rma(dn, n)
    out = np.full(len(close), np.nan)
    ok = ~np.isnan(ru) & ~np.isnan(rd)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ru / rd
        vals = 100 - 100 / (1 + rs)
    out[ok] = vals[ok]
    out[ok & (rd == 0)] = 100.0
    return out


def atr_series(high, low, close, n=14):
    pc = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum.reduce([high - low, np.abs(high - pc), np.abs(low - pc)])
    tr[0] = high[0] - low[0]
    return rma(tr, n)


def weekly_channel(df):
    """Kanał konsolidacji W1 (odpowiednik calc_htf), wartości z poprzedniej, zamkniętej świecy tygodniowej."""
    wk = df.resample("W-SUN", closed="right", label="right").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Close"])
    c, h, l, v = (wk[k].to_numpy(float) for k in ("Close", "High", "Low", "Volume"))
    n = len(wk)

    rsi = rsi_series(c, 14)
    cs = pd.Series(c)
    basis = cs.rolling(20, min_periods=20).mean().to_numpy()
    dev = cs.rolling(20, min_periods=20).std(ddof=0).to_numpy()
    bbw = 4.0 * dev / basis
    avg_bbw = pd.Series(bbw).rolling(20, min_periods=20).mean().to_numpy()
    avg_vol = pd.Series(v).rolling(20, min_periods=20).mean().to_numpy()
    atr = atr_series(h, l, c, ATR_LEN)

    with np.errstate(invalid="ignore"):
        cond = (rsi >= RSI_LOW) & (rsi <= RSI_HIGH) & (bbw < avg_bbw * SQZ_MULT)
        if USE_VOL:
            cond &= v < avg_vol

    up = np.full(n, np.nan)
    lo = np.full(n, np.nan)
    cons = np.zeros(n, dtype=bool)
    cnt = 0
    up_b = lo_b = np.nan
    for k in range(n):
        cnt = cnt + 1 if cond[k] else 0
        cons[k] = cnt >= MIN_BARS
        ln = max(cnt, 2)
        if cons[k] and k - ln + 1 >= 0:
            up_b = h[k - ln + 1:k + 1].max()
            lo_b = l[k - ln + 1:k + 1].min()
        elif RESET_BREAK and k > 0 and (
                c[k] > (up[k - 1] if not np.isnan(up[k - 1]) else 1e10)
                or c[k] < (lo[k - 1] if not np.isnan(lo[k - 1]) else -1e10)):
            up_b = lo_b = np.nan
        up[k], lo[k] = up_b, lo_b

    def shift1(a):
        out = np.full(len(a), np.nan)
        out[1:] = a[:-1]
        return out

    labels = [ts.date() for ts in wk.index]
    return labels, shift1(up), shift1(lo), shift1(cons.astype(float)), shift1(atr)


# ---------------------------------------------------------------- cykl halvingowy

def cycle_info(d):
    last = max((h for h in HALVINGS if h <= d), default=None)
    if last is None:
        return None
    if d >= HALVINGS[4]:
        nxt = HALVINGS[4] + timedelta(days=1461)
    elif d >= HALVINGS[3]:
        nxt = HALVINGS[4]
    elif d >= HALVINGS[2]:
        nxt = HALVINGS[3]
    elif d >= HALVINGS[1]:
        nxt = HALVINGS[2]
    else:
        nxt = HALVINGS[1]
    dd = (d - last).days
    short_end = PEAK_DAY + SHORT_ENTRY_LEN
    long_zone = dd < PEAK_DAY or dd >= BOTTOM_DAY
    short_zone = PEAK_DAY <= dd < short_end
    bear_zone = PEAK_DAY <= dd < BOTTOM_DAY
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

    def tight(rem):
        if rem >= RAMP_DAYS:
            return 0.0
        if rem <= 0:
            return 1.0
        return 1.0 - rem / RAMP_DAYS

    return {
        "dd": dd, "phase": phase, "long_zone": long_zone, "short_zone": short_zone,
        "bear_zone": bear_zone, "rem_peak": rem_peak, "rem_bottom": rem_bottom,
        "mult_long": WIDE_MULT + (TIGHT_MULT - WIDE_MULT) * tight(rem_peak),
        "mult_short": WIDE_MULT + (TIGHT_MULT - WIDE_MULT) * tight(rem_bottom),
        "long_force": FORCE_EXIT and PEAK_DAY + PEAK_GRACE <= dd < BOTTOM_DAY,
        "short_force": FORCE_EXIT and (dd >= BOTTOM_DAY - COVER_MARGIN or dd < PEAK_DAY),
    }


# ---------------------------------------------------------------- replay logiki wskaźnika

def replay(df):
    n = len(df)
    dates = [ts.date() for ts in df.index]
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))

    labels, up_w, lo_w, cons_w, atr_w = weekly_channel(df)
    pos_of = {lab: k for k, lab in enumerate(labels)}
    up_d = np.full(n, np.nan)
    lo_d = np.full(n, np.nan)
    cons_d = np.zeros(n, dtype=bool)
    atr_d = np.full(n, np.nan)
    for i, d in enumerate(dates):
        k = pos_of.get(d + timedelta(days=6 - d.weekday()))
        if k is not None:
            up_d[i], lo_d[i], atr_d[i] = up_w[k], lo_w[k], atr_w[k]
            cons_d[i] = cons_w[k] == 1.0

    sma = pd.Series(c).rolling(SMA_PERIOD, min_periods=SMA_PERIOD).mean().shift(1).to_numpy()

    pos = 0
    stop = ext = entry = last_alert = np.nan
    sig_long_prev = sig_short_prev = False
    events = []
    last = {}
    prev_phase = None

    for i in range(n):
        d = dates[i]
        ci = cycle_info(d)
        if ci is None:
            sig_long_prev = sig_short_prev = False
            continue
        is_up = (not np.isnan(sma[i])) and c[i] > sma[i]
        is_down = (not np.isnan(sma[i])) and c[i] < sma[i]
        atr_use = atr_d[i] if not np.isnan(atr_d[i]) else c[i] * 0.06

        def crossover():
            return (i > 0 and not np.isnan(up_d[i]) and not np.isnan(up_d[i - 1])
                    and c[i] > up_d[i] and c[i - 1] <= up_d[i - 1])

        def crossunder():
            return (i > 0 and not np.isnan(lo_d[i]) and not np.isnan(lo_d[i - 1])
                    and c[i] < lo_d[i] and c[i - 1] >= lo_d[i - 1])

        sig_long = pos == 0 and ci["long_zone"] and (not NEED_SMA or is_up) and crossover()
        sig_short = pos == 0 and ci["short_zone"] and (not NEED_SMA or is_down) and crossunder()

        enter = exit_ = None
        reason = ""
        if pos == 1:
            hit = l[i] < stop
            if hit or (SMA_EXIT and is_down) or ci["long_force"]:
                exit_ = "LONG"
                reason = "stop" if hit else ("zmiana trendu (SMA)" if (SMA_EXIT and is_down) else "koniec fazy cyklu")
                exit_px = stop if hit else c[i]
                pos = 0
            else:
                ext = max(ext, h[i])
                a = ext - ci["mult_long"] * atr_use
                b = (sma[i] if not np.isnan(sma[i]) else 0.0) * (1 - SMA_BUF / 100) if USE_SMA_STOP else 0.0
                stop = max(stop, a, b)
        elif pos == -1:
            hit = h[i] > stop
            if hit or (SMA_EXIT and is_up) or ci["short_force"]:
                exit_ = "SHORT"
                reason = "stop" if hit else ("zmiana trendu (SMA)" if (SMA_EXIT and is_up) else "koniec fazy cyklu")
                exit_px = stop if hit else c[i]
                pos = 0
            else:
                ext = min(ext, l[i])
                a = ext + ci["mult_short"] * atr_use
                b = (sma[i] if not np.isnan(sma[i]) else 1e12) * (1 + SMA_BUF / 100) if USE_SMA_STOP else 1e12
                stop = min(stop, a, b)
        else:
            if sig_long_prev:
                enter, pos, entry, ext = "LONG", 1, o[i], h[i]
                fb = o[i] - ci["mult_long"] * atr_use
                cc = lo_d[i - 1] * (1 - CH_BUF / 100) if i > 0 and not np.isnan(lo_d[i - 1]) else np.nan
                s0 = cc if (USE_CH_STOP and not np.isnan(cc) and cc < o[i]) else fb
                stop = max(s0, o[i] * (1 - MAX_LOSS_PCT / 100))
                last_alert = stop
            elif sig_short_prev:
                enter, pos, entry, ext = "SHORT", -1, o[i], l[i]
                fb = o[i] + ci["mult_short"] * atr_use
                cc = up_d[i - 1] * (1 + CH_BUF / 100) if i > 0 and not np.isnan(up_d[i - 1]) else np.nan
                s0 = cc if (USE_CH_STOP and not np.isnan(cc) and cc > o[i]) else fb
                stop = min(s0, o[i] * (1 + MAX_LOSS_PCT / 100))
                last_alert = stop

        if i >= n - EVENT_WINDOW:
            iso = d.isoformat()
            if enter:
                events.append({"type": f"ENTER_{enter}", "date": iso, "price": float(o[i]), "stop": float(stop)})
            if exit_:
                events.append({"type": f"EXIT_{exit_}", "date": iso, "price": float(exit_px), "reason": reason})
            if prev_phase is not None and ci["phase"] != prev_phase:
                events.append({"type": "PHASE", "date": iso, "phase": ci["phase"]})
        if pos != 0 and not enter and not np.isnan(stop) and not np.isnan(last_alert):
            if abs(stop - last_alert) / last_alert * 100 >= MOVE_STEP:
                if i >= n - EVENT_WINDOW:
                    events.append({"type": "MOVE", "date": d.isoformat(), "side": "LONG" if pos == 1 else "SHORT",
                                   "stop": float(stop)})
                last_alert = stop

        prev_phase = ci["phase"]
        sig_long_prev, sig_short_prev = sig_long, sig_short
        last = {"i": i, "date": d, "ci": ci, "pos": pos, "stop": stop, "entry": entry, "close": c[i],
                "sma": sma[i], "is_up": is_up, "up": up_d[i], "lo": lo_d[i], "cons": bool(cons_d[i]),
                "atr": atr_use, "sig_long": sig_long, "sig_short": sig_short}
    return last, events


# ---------------------------------------------------------------- formatowanie

def usd(v):
    return "$" + f"{v:,.0f}".replace(",", NBSP)


def pct(v, d=1):
    return f"{v:+.{d}f}%"


def fmt_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


def event_text(e):
    t = e["type"]
    if t == "ENTER_LONG":
        return (f"🟢 <b>WEJŚCIE LONG</b> - otwarcie świecy {fmt_date(e['date'])}, cena {usd(e['price'])}, "
                f"SL startowy {usd(e['stop'])}")
    if t == "ENTER_SHORT":
        return (f"🔴 <b>WEJŚCIE SHORT</b> - otwarcie świecy {fmt_date(e['date'])}, cena {usd(e['price'])}, "
                f"SL startowy {usd(e['stop'])}")
    if t == "EXIT_LONG":
        return (f"🟩 <b>ZIELONY X - zamknij LONG</b> ({e['reason']}), ok. {usd(e['price'])} "
                f"[{fmt_date(e['date'])}]")
    if t == "EXIT_SHORT":
        return (f"🟥 <b>CZERWONY X - zamknij SHORT</b> ({e['reason']}), ok. {usd(e['price'])} "
                f"[{fmt_date(e['date'])}]")
    if t == "MOVE":
        return f"📐 <b>Przesuń SL {e['side']}</b> na {usd(e['stop'])} [{fmt_date(e['date'])}]"
    if t == "PHASE":
        return f"🔄 <b>Zmiana fazy cyklu:</b> {e['phase']} [{fmt_date(e['date'])}]"
    return t


def build_message(today, spot, last, new_events, source, partial):
    ci = last["ci"]
    pos = last["pos"]
    msg = f"<b>₿ BTC - Strefy konsolidacji</b> {today.strftime('%d.%m.%Y')}\n"
    msg += f"Cena: <b>{usd(spot)}</b> | wykres D1, kanały W1 | dane: {source}\n"
    if partial:
        msg += "<i>Stan uwzględnia dzisiejszą, jeszcze otwartą świecę dzienną.</i>\n"
    msg += "\n"

    if new_events:
        msg += "<b>NOWE SYGNAŁY:</b>\n"
        for e in new_events:
            msg += event_text(e) + "\n"
        msg += "\n"

    sma = last["sma"]
    trend_ic = "🟢" if last["is_up"] else "🔴"
    msg += "<b>Status:</b>\n"
    zone_txt = ("tylko LONG" if ci["long_zone"] else "tylko SHORT" if ci["short_zone"] else "bez nowych wejść")
    msg += (f"🔄 <b>Faza cyklu</b>: {ci['phase']} ({zone_txt}), {ci['dd']} dni od halvingu\n")
    if not np.isnan(sma):
        msg += (f"{trend_ic} <b>Trend SMA 200</b>: {'wzrostowy' if last['is_up'] else 'spadkowy'}: "
                f"cena {pct((spot / sma - 1) * 100)} vs {usd(sma)}\n")
    up, lo = last["up"], last["lo"]
    if not np.isnan(up) and not np.isnan(lo):
        msg += (f"📏 <b>Kanał W1</b>: opór {usd(up)} ({pct((up / spot - 1) * 100)}), "
                f"wsparcie {usd(lo)} ({pct((lo / spot - 1) * 100)})"
                f"{' - konsolidacja aktywna' if last['cons'] else ''}\n")
    else:
        msg += "📏 <b>Kanał W1</b>: brak aktywnego kanału\n"

    if pos != 0:
        side = "LONG" if pos == 1 else "SHORT"
        stop, entry = last["stop"], last["entry"]
        dist = abs(spot - stop) / spot * 100
        liq = entry * (1 - 1 / LEVERAGE + MMR_PCT / 100) if pos == 1 else entry * (1 + 1 / LEVERAGE - MMR_PCT / 100)
        sl_to_entry = abs(entry - stop) / entry * 100
        max_lev = 100 / (sl_to_entry * 1.5) if sl_to_entry > 0 else None
        mult = ci["mult_long"] if pos == 1 else ci["mult_short"]
        rem = ci["rem_peak"] if pos == 1 else ci["rem_bottom"]
        rem_txt = f"{rem:.0f} dni do {'szczytu' if pos == 1 else 'dołka'}"
        msg += f"{'🟢' if pos == 1 else '🔴'} <b>Pozycja {side}</b>: wejście {usd(entry)}\n"
        msg += f"🛡 <b>Ustaw SL na</b>: {usd(stop)} (odległość od ceny {dist:.1f}%)\n"
        msg += f"⚙️ Mnożnik ATR {mult:.1f}x ({rem_txt})\n"
        msg += f"💀 Likwidacja x{LEVERAGE:g}: ok. {usd(liq)}"
        if max_lev is not None:
            msg += f" | maks. bezpieczna dźwignia dla tego SL: {max_lev:.1f}x"
        msg += "\n"
        if max_lev is not None and max_lev < LEVERAGE:
            msg += f"⚠️ Przy dźwigni {LEVERAGE:g}x stop jest za daleko - ryzyko likwidacji przed SL.\n"
    else:
        msg += "⚪️ <b>Pozycja</b>: brak\n"
        if ci["long_zone"] and last["is_up"] and not np.isnan(up):
            msg += f"👀 Czekam na LONG: zamknięcie dzienne powyżej oporu {usd(up)} ({pct((up / spot - 1) * 100)}).\n"
        elif ci["long_zone"] and not last["is_up"]:
            msg += "👀 Faza pozwala na LONG, ale cena jest pod SMA 200 - czekam na jej odzyskanie.\n"
        elif ci["short_zone"] and not last["is_up"] and not np.isnan(lo):
            msg += f"👀 Czekam na SHORT: zamknięcie dzienne poniżej wsparcia {usd(lo)} ({pct((lo / spot - 1) * 100)}).\n"
        elif ci["short_zone"] and last["is_up"]:
            msg += "👀 Faza pozwala na SHORT, ale cena jest nad SMA 200 - czekam na jej utratę.\n"
        else:
            msg += "👀 Faza cyklu nie dopuszcza nowych wejść.\n"

    msg += ("\n<i>Odtworzenie wskaźnika TradingView na danych yfinance - sygnały mogą różnić się od wykresu. "
            "To nie jest porada inwestycyjna.</i>")
    return msg


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki i bez zapisu stanu")
    parser.add_argument("--force", action="store_true", help="wyślij komunikat mimo braku nowych zdarzeń")
    args = parser.parse_args()

    df, source = load_daily()
    if len(df) < 400:
        sys.exit("Za mało danych dziennych - przerywam.")

    today = datetime.now(timezone.utc).date()
    partial = df.index[-1].date() == today
    spot = float(df["Close"].iloc[-1])

    last, events = replay(df)
    if not last:
        sys.exit("Replay nie zwrócił stanu - przerywam.")
    print(f"Stan: poz={last['pos']}, SL={last['stop']}, faza={last['ci']['phase']}, zdarzeń={len(events)}")

    state = load_json(STATE_FILE, {})
    notified = list(state.get("notified", []))
    keyed = [(f"{e['type']}|{e['date']}|{e.get('side', '')}", e) for e in events]
    new_events = [e for k, e in keyed if k not in notified]

    today_iso = today.isoformat()
    summary_due = state.get("last_summary_date") != today_iso
    message = build_message(today, spot, last, new_events, source, partial)
    print(message)

    if args.dry_run:
        return
    if not (new_events or summary_due or args.force):
        print("Brak nowych sygnałów i podsumowanie już wysłane - nic nie wysyłam.")
        return

    TG.require()
    TG.send_message(message)
    state["notified"] = (notified + [k for k, _ in keyed])[-NOTIFIED_KEEP:]
    state["last_summary_date"] = today_iso
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
