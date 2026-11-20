"""
04_BTC_Charts.py

Cztery wykresy BTC/USDT wysylane na Telegram, kazdy OSOBNYM zdjeciem, w kolejnosci:
M1 (miesieczny), W1 (tygodniowy), D1 (dzienny), H4 (4-godzinny).
Wykres: swiece + SMA 50, pod nim osobno wolumen, pod nim RSI 14 (miedzy panelami jest przerwa).
Strefy SMC/ICT rysowane tylko, gdy mieszcza sie w zakresie cen widocznych swiec:
  - FVG (Fair Value Gap)      - 3-swiecowa luka: low[i] > high[i-2] (bycza) / high[i] < low[i-2] (niedzwiedzia)
  - OB  (Order Block)         - ostatnia swieca przeciwna przed impulsem zamykajacym sie poza jej zakresem
  - RB  (Rejection Block)     - dlugi knot na swing high/low (od ciala do ekstremum)
Na wykresie nie ma opisow wskaznikow - przy liniach oporu stoi tylko wartosc. Wszystkie
szczegoly (SMA, RSI, trend, opor, strefy) sa w podpisie pod wykresem.

Podpis pod kazdym wykresem ma ten sam uklad: cena, SMA 50, RSI 14, trend, opor, FVG, OB, RB.
Zakres wykresu: M1/W1/D1 ostatnie 12 miesiecy, H4 ostatnie 30 dni. SMA, RSI, trend i strefy
liczone na pelnej pobranej historii, dopiero potem wykres jest przycinany. Pokazywane sa
tylko strefy AKTYWNE.

Trend (punkty -3..+3): struktura swingow (HH/HL = +1, LH/LL = -1), cena vs SMA 50 (+1 / -1),
nachylenie SMA 50 wzgledem 10 swiec wczesniej (+1 / -1). Wynik >= +2 = WZROSTOWY,
<= -2 = SPADKOWY, w pozostalych przypadkach BOCZNY.

Opor zalezy od trendu:
  - WZROSTOWY -> najsilniejszy opor wzrostu: poziom NAD cena (sufit, ktory moze zatrzymac wzrost),
  - SPADKOWY  -> najsilniejszy opor spadku: poziom POD cena (dolny, ktory moze zatrzymac spadek),
  - BOCZNY    -> oba: opor gorny i opor dolny.
Sila poziomu = konfluencja (suma wag nakladajacych sie kandydatow): OB 3, RB 2, SMA 50 2,
FVG 1, swing high/low 1. Nad cena licza sie strefy niedzwiedzie (↓), pod cena bycze (↑).
Remis rozstrzyga poziom blizszy cenie. To poziom orientacyjny, nie gwarancja zatrzymania ruchu.

Zrodlo glowne: OKX (BTC-USDT spot, swiece UTC: 1Dutc / 1Wutc / 1Mutc, 4H), bez kluczy API.
OKX domyslnie otwiera swiece D1/W1/M1 o polnocy UTC+8 (16:00 UTC), dlatego uzywamy
wariantow *utc. Zapas: Kraken XBT/USD (H4, D1, W1; M1 skladane z W1 - przyblizenie).
Zrodlo jest zapisywane tylko w logu. Swieca biezaca (w trakcie) jest pokazywana.
Wykresy rysuje sam matplotlib (bez mplfinance).

Zmienne srodowiskowe:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  TELEGRAM_THREAD_WYKRESY   - ID tematu #WYKRESY (93)

Uruchomienie lokalne bez wysylki:  python 04_BTC_Charts.py --dry-run
"""

import sys
import time
import argparse
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from common import TZ, Telegram, fmt_usd, http_get

TG = Telegram("TELEGRAM_THREAD_WYKRESY")  # ID tematu #WYKRESY (93)

OUT_DIR = Path("tmp_charts")

OKX_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_INST = "BTC-USDT"
OKX_PAGE = 100

SMA_PERIOD = 50
RSI_PERIOD = 14
RSI_HIGH, RSI_LOW = 70, 30
WARMUP = 150            # swiece ponad okno wykresu: zbieznosc RSI/SMA + historia stref
OHLC_COLS = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}

# Parametry stref
ATR_PERIOD = 14
FVG_MIN_ATR = 0.25      # minimalna wysokosc FVG w ATR (odsiew szumu)
OB_DISP_ATR = 1.0       # korpus swiecy impulsu >= 1 ATR
RB_SWING = 3            # swing: ekstremum wzgledem 3 swiec z kazdej strony
RB_WICK_RANGE = 0.5     # knot >= 50% zakresu swiecy
RB_WICK_BODY = 2.0      # knot >= 2 x korpus
RB_MIN_ATR = 0.5        # knot >= 0.5 ATR
MAX_ZONES = 3           # tyle najnowszych aktywnych stref kazdego typu

# Trend i opor
TREND_SWING = 5         # swing do oceny struktury: ekstremum wzgledem 5 swiec z kazdej strony
SMA_SLOPE_BARS = 10     # nachylenie SMA 50: porownanie z wartoscia sprzed 10 swiec
RES_TOL_ATR = 0.5       # poziomy w odleglosci <= 0.5 ATR traktowane jako konfluencja
RES_SWINGS = 5          # ile ostatnich swingow bierzemy jako kandydatow
RES_WEIGHTS = {"OB": 3, "RB": 2, "SMA": 2, "FVG": 1, "SWING": 1}

# (trend, strona) -> opis w podpisie
LEVEL_TAGS = {
    ("WZROSTOWY", "up"): "Najsilniejszy opór wzrostu",
    ("SPADKOWY", "down"): "Najsilniejszy opór spadku (dolny)",
    ("BOCZNY", "up"): "Opór górny",
    ("BOCZNY", "down"): "Opór dolny",
}
LEVEL_COLOR = {"up": "#d62728", "down": "#2ca02c"}

ZONE_STYLE = {          # (kolor, etykieta)
    ("FVG", 1): ("#2ca02c", "FVG"), ("FVG", -1): ("#d62728", "FVG"),
    ("OB", 1): ("#1f77b4", "OB"), ("OB", -1): ("#ff7f0e", "OB"),
    ("RB", 1): ("#9467bd", "RB"), ("RB", -1): ("#8c564b", "RB"),
}

# Wyglad wykresu
UP_COLOR, DOWN_COLOR = "#26a69a", "#ef5350"
SMA_COLOR, RSI_COLOR = "#1f77b4", "#7f3fbf"
PANEL_GAP = 0.22        # odstep miedzy panelami (cena / wolumen / RSI)
Y_PAD = 0.04            # zapas osi Y ponad najwyzsza / ponizej najnizszej widocznej swiecy
X_TICKS = 8
DATE_FMT = {"M1": "%Y-%m", "W1": "%d.%m.%y", "D1": "%d.%m.%y", "H4": "%d.%m %H:%M"}

# etykieta -> (bar OKX, interwal Kraken [min], zakres widoczny na wykresie, ile swiec pobrac)
# Kolejnosc = kolejnosc wysylki.
CHARTS = {
    "M1": ("1Mutc", None, {"months": 12}, 12 + WARMUP),
    "W1": ("1Wutc", 10080, {"months": 12}, 53 + WARMUP),
    "D1": ("1Dutc", 1440, {"months": 12}, 365 + WARMUP),
    "H4": ("4H", 240, {"days": 30}, 6 * 30 + WARMUP),
}


# ---------------------------------------------------------------- dane

def fetch_okx(bar, need):
    rows, after = [], None
    while len(rows) < need:
        params = {"instId": OKX_INST, "bar": bar, "limit": OKX_PAGE}
        if after:
            params["after"] = after          # rekordy starsze niz ts
        data = http_get(OKX_URL, params, user_agent="btc-charts-bot")
        if data.get("code") != "0":
            raise RuntimeError(f"OKX: {data.get('code')} {data.get('msg')}")
        batch = data["data"]
        if not batch:
            break
        rows += batch
        after = batch[-1][0]
        time.sleep(0.2)                       # limit 20 zapytan / 2 s
    df = pd.DataFrame([r[:6] for r in rows],
                      columns=["t", "Open", "High", "Low", "Close", "Volume"])
    df.index = pd.to_datetime(df["t"].astype("int64"), unit="ms")
    df = df[list(OHLC_COLS)].astype(float).sort_index()
    return df[~df.index.duplicated()]


def fetch_kraken(interval):
    data = http_get("https://api.kraken.com/0/public/OHLC",
                    {"pair": "XBTUSD", "interval": interval}, user_agent="btc-charts-bot")
    if data.get("error"):
        raise RuntimeError(data["error"])
    result = data["result"]
    key = next(k for k in result if k != "last")
    df = pd.DataFrame(result[key], columns=["t", "Open", "High", "Low", "Close",
                                            "vwap", "Volume", "count"])
    df.index = pd.to_datetime(df["t"], unit="s")
    return df[list(OHLC_COLS)].astype(float).sort_index()


def get_candles(label):
    bar, kraken_interval, _, need = CHARTS[label]
    try:
        df = fetch_okx(bar, need)
        if len(df) < SMA_PERIOD + 5:
            raise RuntimeError(f"za mało świec z OKX ({len(df)})")
        return df, f"OKX {OKX_INST} (świece UTC)"
    except Exception as e:  # noqa: BLE001
        print(f"[BLAD] OKX {label}: {e} - przechodzę na Kraken")
    if label == "M1":
        df = fetch_kraken(10080).resample("MS").agg(OHLC_COLS).dropna()
        return df, "Kraken XBT/USD (fallback, M1 złożone z W1 - przybliżenie)"
    return fetch_kraken(kraken_interval), "Kraken XBT/USD (fallback)"


# ---------------------------------------------------------------- wskazniki

def rsi_wilder(close, n=RSI_PERIOD):
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def atr_series(df, n=ATR_PERIOD):
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(),
                    (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def swing_points(df, k=TREND_SWING):
    """Potwierdzone szczyty i dołki swingowe: listy (indeks, cena)."""
    h, l = df["High"].to_numpy(), df["Low"].to_numpy()
    highs, lows = [], []
    for i in range(k, len(df) - k):
        if h[i] >= h[i - k:i + k + 1].max():
            highs.append((i, float(h[i])))
        if l[i] <= l[i - k:i + k + 1].min():
            lows.append((i, float(l[i])))
    return highs, lows


# ---------------------------------------------------------------- strefy (FVG / OB / RB)

def _first_break(close, high, low, start, kind, bottom, top, direction):
    """Indeks pierwszej swiecy po `start`, ktora uniewaznia strefe (None = aktywna)."""
    for j in range(start, len(close)):
        if kind == "FVG":        # luka wypelniona w calosci
            if direction == 1 and low[j] <= bottom:
                return j
            if direction == -1 and high[j] >= top:
                return j
        else:                    # OB / RB: zamkniecie za przeciwna krawedzia
            if direction == 1 and close[j] < bottom:
                return j
            if direction == -1 and close[j] > top:
                return j
    return None


def detect_zones(df):
    """Zwraca liste aktywnych stref: {kind, dir, start, bottom, top}."""
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    atr = atr_series(df).to_numpy()
    n = len(df)
    zones = []

    def add(kind, direction, start, bottom, top, check_from):
        if _first_break(c, h, l, check_from, kind, bottom, top, direction) is None:
            zones.append({"kind": kind, "dir": direction, "start": start,
                          "bottom": float(bottom), "top": float(top)})

    # FVG: swiece i-2, i-1 (impuls), i
    for i in range(2, n):
        if np.isnan(atr[i]):
            continue
        if l[i] > h[i - 2] and (l[i] - h[i - 2]) >= FVG_MIN_ATR * atr[i]:
            add("FVG", 1, i - 1, h[i - 2], l[i], i + 1)
        elif h[i] < l[i - 2] and (l[i - 2] - h[i]) >= FVG_MIN_ATR * atr[i]:
            add("FVG", -1, i - 1, h[i], l[i - 2], i + 1)

    # OB: swieca k przeciwna + impuls k+1 zamykajacy sie poza jej zakresem
    for k in range(0, n - 1):
        a = atr[k + 1]
        if np.isnan(a):
            continue
        body = abs(c[k + 1] - o[k + 1])
        if c[k] < o[k] and c[k + 1] > o[k + 1] and c[k + 1] > h[k] and body >= OB_DISP_ATR * a:
            add("OB", 1, k, l[k], h[k], k + 2)
        elif c[k] > o[k] and c[k + 1] < o[k + 1] and c[k + 1] < l[k] and body >= OB_DISP_ATR * a:
            add("OB", -1, k, l[k], h[k], k + 2)

    # RB: swing high/low z dlugim knotem
    for i in range(RB_SWING, n - RB_SWING):
        if np.isnan(atr[i]):
            continue
        rng = h[i] - l[i]
        if rng <= 0:
            continue
        body = abs(c[i] - o[i])
        win = slice(i - RB_SWING, i + RB_SWING + 1)
        up_wick = h[i] - max(o[i], c[i])
        lo_wick = min(o[i], c[i]) - l[i]
        if (h[i] >= h[win].max() and up_wick >= RB_WICK_RANGE * rng
                and up_wick >= RB_WICK_BODY * body and up_wick >= RB_MIN_ATR * atr[i]):
            add("RB", -1, i, max(o[i], c[i]), h[i], i + 1)
        elif (l[i] <= l[win].min() and lo_wick >= RB_WICK_RANGE * rng
                and lo_wick >= RB_WICK_BODY * body and lo_wick >= RB_MIN_ATR * atr[i]):
            add("RB", 1, i, l[i], min(o[i], c[i]), i + 1)

    # po MAX_ZONES najnowszych aktywnych stref kazdego typu
    picked = []
    for kind in ("FVG", "OB", "RB"):
        z = sorted((z for z in zones if z["kind"] == kind), key=lambda x: x["start"])
        picked += z[-MAX_ZONES:]
    return picked


# ---------------------------------------------------------------- trend i opor

def detect_trend(df):
    """Zwraca (etykieta, ikona, opis). Wejscie: pelna historia z kolumna SMA."""
    price = df["Close"].iloc[-1]
    sma = df["SMA"]
    highs, lows = swing_points(df)
    pts, parts = 0, []

    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = highs[-1][1] > highs[-2][1], lows[-1][1] > lows[-2][1]
        if hh and hl:
            pts += 1
            parts.append("HH/HL")
        elif not hh and not hl:
            pts -= 1
            parts.append("LH/LL")
        else:
            parts.append("struktura mieszana")

    s = sma.iloc[-1]
    if pd.notna(s):
        above = price > s
        pts += 1 if above else -1
        parts.append(f"cena {'powyżej' if above else 'poniżej'} SMA {SMA_PERIOD}")
        if len(sma) > SMA_SLOPE_BARS and pd.notna(sma.iloc[-1 - SMA_SLOPE_BARS]):
            rising = s > sma.iloc[-1 - SMA_SLOPE_BARS]
            pts += 1 if rising else -1
            parts.append(f"SMA {'rośnie' if rising else 'spada'}")

    if pts >= 2:
        return "WZROSTOWY", "🟢", ", ".join(parts)
    if pts <= -2:
        return "SPADKOWY", "🔴", ", ".join(parts)
    return "BOCZNY", "🟡", ", ".join(parts)


def find_level(df, zones, side):
    """
    Najsilniejszy poziom (konfluencja wag): side='up' = nad cena (opor gorny),
    side='down' = pod cena (opor dolny). Zwraca dict lub None.
    """
    up = side == "up"
    price = df["Close"].iloc[-1]
    atr = atr_series(df).iloc[-1]
    tol = RES_TOL_ATR * atr if pd.notna(atr) else 0.0

    cands = []  # (dol, gora, waga, nazwa)
    for z in zones:
        if up and z["dir"] == -1 and z["top"] > price:
            cands.append((z["bottom"], z["top"], RES_WEIGHTS[z["kind"]], f"{z['kind']}↓"))
        elif not up and z["dir"] == 1 and z["bottom"] < price:
            cands.append((z["bottom"], z["top"], RES_WEIGHTS[z["kind"]], f"{z['kind']}↑"))
    s = df["SMA"].iloc[-1]
    if pd.notna(s) and ((up and s > price) or (not up and s < price)):
        cands.append((s, s, RES_WEIGHTS["SMA"], f"SMA {SMA_PERIOD}"))
    highs, lows = swing_points(df)
    for _, p in (highs if up else lows)[-RES_SWINGS:]:
        if (up and p > price) or (not up and p < price):
            cands.append((p, p, RES_WEIGHTS["SWING"], "szczyt" if up else "dołek"))
    if not cands:
        return None

    best = None
    for lo, hi, _, _ in cands:
        members = [c for c in cands if c[0] <= hi + tol and c[1] >= lo - tol]
        strength = sum(c[2] for c in members)
        bottom = min(c[0] for c in members)
        top = max(c[1] for c in members)
        names = list(dict.fromkeys(c[3] for c in sorted(members, key=lambda c: -c[2])))
        dist = max(bottom - price, 0.0) if up else max(price - top, 0.0)
        key = (strength, -dist)
        if best is None or key > best[0]:
            sign = 1 if up else -1
            best = (key, {"side": side, "bottom": bottom, "top": top, "names": names,
                          "dist_pct": sign * dist / price * 100})
    return best[1]


# ---------------------------------------------------------------- przygotowanie danych

def prepare(df, lookback):
    """Wskazniki, trend, poziomy oporu i strefy na pelnej historii, potem przyciecie do okna."""
    df = df.copy()
    df["SMA"] = df["Close"].rolling(SMA_PERIOD).mean()
    df["RSI"] = rsi_wilder(df["Close"])
    zones = detect_zones(df)
    trend = detect_trend(df)
    sides = {"WZROSTOWY": ["up"], "SPADKOWY": ["down"]}.get(trend[0], ["up", "down"])
    levels = [(side, find_level(df, zones, side)) for side in sides]
    start_ts = df.index[-1] - pd.DateOffset(**lookback)
    cut = df.index.get_indexer([df.index[df.index > start_ts][0]])[0]
    view = df.iloc[cut:]
    for z in zones:
        z["start"] = max(z["start"] - cut, 0)       # pozycja wzgledem widocznego okna
    return view, zones, trend, levels


# ---------------------------------------------------------------- wykres

def num(v):
    """Liczba bez znaku $ (matplotlib traktuje $...$ jako mathtext)."""
    return f"{v:,.0f}".replace(",", " ")


def short_num(v, _pos=None):
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}"


def level_range(lvl, fmt):
    lo, hi = lvl["bottom"], lvl["top"]
    return fmt(lo) if round(lo) == round(hi) else f"{fmt(lo)} - {fmt(hi)}"


def visible_zones(zones, ylo, yhi):
    """Tylko strefy, ktore choc czesciowo leza w zakresie cen widocznych swiec."""
    return [z for z in zones if z["top"] >= ylo and z["bottom"] <= yhi]


def build_chart(df, zones, trend, levels, label, path):
    n = len(df)
    x = np.arange(n)
    lo, hi = float(df["Low"].min()), float(df["High"].max())
    pad = (hi - lo) * Y_PAD
    ylo, yhi = lo - pad, hi + pad

    fig = plt.figure(figsize=(14, 10), facecolor="white")
    gs = fig.add_gridspec(3, 1, height_ratios=(6, 1.5, 2.2), hspace=PANEL_GAP)
    ax_p = fig.add_subplot(gs[0])
    ax_v = fig.add_subplot(gs[1], sharex=ax_p)
    ax_r = fig.add_subplot(gs[2], sharex=ax_p)

    # --- swiece
    up = (df["Close"] >= df["Open"]).to_numpy()
    colors = np.where(up, UP_COLOR, DOWN_COLOR)
    body_lo = np.minimum(df["Open"].to_numpy(), df["Close"].to_numpy())
    body_h = np.maximum(np.abs(df["Close"].to_numpy() - df["Open"].to_numpy()), (yhi - ylo) * 0.002)
    ax_p.vlines(x, df["Low"].to_numpy(), df["High"].to_numpy(), colors=colors, linewidth=1.0, zorder=3)
    ax_p.bar(x, body_h, bottom=body_lo, width=0.7, color=colors, zorder=4)
    ax_p.plot(x, df["SMA"].to_numpy(), color=SMA_COLOR, linewidth=1.4, zorder=5)

    # --- strefy (tylko w zakresie cen, przyciete do osi)
    for z in visible_zones(zones, ylo, yhi):
        color, text = ZONE_STYLE[(z["kind"], z["dir"])]
        bottom, top = max(z["bottom"], ylo), min(z["top"], yhi)
        ax_p.fill_between(x[z["start"]:], bottom, top, color=color, alpha=0.18, linewidth=0, zorder=1)
        ax_p.text(z["start"], top, text, color=color, fontsize=8, fontweight="bold",
                  va="top", ha="left", zorder=6)

    # --- linie oporu: tylko wartosc przy linii, tylko gdy w zakresie
    for side, lvl in levels:
        if not lvl:
            continue
        color = LEVEL_COLOR[side]
        pair = lvl["bottom"] != lvl["top"] and round(lvl["bottom"]) != round(lvl["top"])
        for value, va in ((lvl["bottom"], "top" if pair else "bottom"), (lvl["top"], "bottom")):
            if not (ylo <= value <= yhi):
                continue
            ax_p.axhline(value, color=color, linestyle="--", linewidth=1.2, zorder=2)
            ax_p.text(n - 0.5, value, num(value), color=color, fontsize=9, fontweight="bold",
                      va=va, ha="right", zorder=6)
            if not pair:
                break

    ax_p.set_ylim(ylo, yhi)
    ax_p.set_title(f"BTC {label}", loc="left", fontsize=12, fontweight="bold")

    # --- wolumen (osobny panel)
    ax_v.bar(x, df["Volume"].to_numpy(), width=0.7, color=colors, alpha=0.6)
    ax_v.yaxis.set_major_formatter(FuncFormatter(short_num))

    # --- RSI (osobny panel)
    ax_r.plot(x, df["RSI"].to_numpy(), color=RSI_COLOR, linewidth=1.2)
    for level in (RSI_HIGH, RSI_LOW):
        ax_r.axhline(level, color="gray", linestyle="--", linewidth=0.7)
    ax_r.set_ylim(0, 100)
    ax_r.set_yticks([RSI_LOW, RSI_HIGH])

    # --- osie
    ax_p.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: num(v)))
    for ax in (ax_p, ax_v, ax_r):
        ax.yaxis.tick_right()
        ax.grid(alpha=0.25)
        ax.set_facecolor("white")
    ax_p.set_xlim(-1, n + max(1.0, n * 0.03))

    ticks = np.unique(np.linspace(0, n - 1, min(X_TICKS, n)).round().astype(int))
    ax_r.set_xticks(ticks)
    ax_r.set_xticklabels(df.index[ticks].strftime(DATE_FMT[label]), fontsize=9)
    ax_p.tick_params(labelbottom=False)
    ax_v.tick_params(labelbottom=False)

    fig.subplots_adjust(left=0.04, right=0.93, top=0.95, bottom=0.06)
    fig.savefig(str(path), dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- formatowanie

def usd(v):
    return fmt_usd(v, 0)


def build_caption(label, df, zones, trend, levels, today_iso):
    """Ten sam uklad dla kazdego interwalu (M1 / W1 / D1 / H4)."""
    last = df.iloc[-1]
    price = last["Close"]

    if pd.notna(last["SMA"]):
        dev = (price / last["SMA"] - 1) * 100
        sma_txt = f"{'🟢' if dev > 0 else '🔴'} SMA {SMA_PERIOD}: {usd(last['SMA'])} (cena {dev:+.1f}%)"
    else:
        sma_txt = f"⚪️ SMA {SMA_PERIOD}: N/D"

    r = last["RSI"]
    if pd.notna(r):
        state = "wykupienie" if r > RSI_HIGH else ("wyprzedanie" if r < RSI_LOW else "neutralnie")
        icon = "🔴" if r > RSI_HIGH else ("🟢" if r < RSI_LOW else "🟡")
        rsi_txt = f"{icon} RSI {RSI_PERIOD}: {r:.1f} ({state})"
    else:
        rsi_txt = f"⚪️ RSI {RSI_PERIOD}: N/D"

    t_label, t_icon, t_why = trend
    trend_txt = f"{t_icon} <b>Trend</b>: {t_label}" + (f" ({t_why})" if t_why else "")

    level_lines = []
    for side, lvl in levels:
        tag = LEVEL_TAGS[(t_label, side)]
        if lvl:
            level_lines.append(f"🧱 <b>{tag}</b>: {level_range(lvl, usd)} "
                               f"({lvl['dist_pct']:+.1f}%) - {' + '.join(lvl['names'])}")
        else:
            level_lines.append(f"🧱 <b>{tag}</b>: brak poziomów {'nad' if side == 'up' else 'pod'} ceną")

    lines = []
    for kind in ("FVG", "OB", "RB"):
        kz = [z for z in zones if z["kind"] == kind]
        if not kz:
            lines.append(f"<b>{kind}</b>: brak aktywnych")
            continue
        near = min(kz, key=lambda z: min(abs(price - z["bottom"]), abs(price - z["top"])))
        arrow = "↑" if near["dir"] == 1 else "↓"
        lines.append(f"<b>{kind}</b>: {len(kz)} akt. | najbliższa {arrow} "
                     f"{usd(near['bottom'])} - {usd(near['top'])}")

    return (f"<b>₿ BTC - wykres {label}</b> {today_iso}\n"
            f"Cena: <b>{usd(price)}</b> | świec: {len(df)} (ostatnia w trakcie)\n"
            f"{sma_txt}\n{rsi_txt}\n{trend_txt}\n" + "\n".join(level_lines) + "\n" + "\n".join(lines))


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki na Telegram")
    args = parser.parse_args()

    if not args.dry_run:
        TG.require()

    today_iso = datetime.now(TZ).strftime("%d.%m.%Y")
    OUT_DIR.mkdir(exist_ok=True)

    charts = []
    for label, (_, _, lookback, _) in CHARTS.items():
        raw, source = get_candles(label)
        print(f"[{label}] źródło danych: {source}")      # tylko log, nie Telegram
        df, zones, trend, levels = prepare(raw, lookback)
        if df.empty:
            sys.exit(f"Brak danych dla {label} - przerywam.")
        path = OUT_DIR / f"BTC_{label}.png"
        build_chart(df, zones, trend, levels, label, path)
        caption = build_caption(label, df, zones, trend, levels, today_iso)
        print(caption)
        charts.append((path, caption))

    if args.dry_run:
        print(f"Dry-run: wykresy zapisane w {OUT_DIR}/, bez wysyłki.")
        return

    for path, caption in charts:                          # kazdy wykres osobno
        TG.send_photo(path, caption)
        time.sleep(1)


if __name__ == "__main__":
    main()
