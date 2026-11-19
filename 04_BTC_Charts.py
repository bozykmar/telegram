"""
04_BTC_Charts.py

Trzy wykresy BTC/USDT wysylane na Telegram, kazdy OSOBNYM zdjeciem: D1, W1, M1.
Na kazdym wykresie: swiece + wolumen + SMA 50 + panel RSI 14 + strefy SMC/ICT:
  - FVG (Fair Value Gap)      - 3-swiecowa luka: low[i] > high[i-2] (bycza) / high[i] < low[i-2] (niedzwiedzia)
  - OB  (Order Block)         - ostatnia swieca przeciwna przed impulsem zamykajacym sie poza jej zakresem
  - RB  (Rejection Block)     - dlugi knot na swing high/low (od ciala do ekstremum)
Podpis pod kazdym wykresem ma ten sam uklad: cena, SMA 50, RSI 14, trend, najsilniejszy opor,
FVG, OB, RB.
Zakres wykresu: ostatnie 12 miesiecy. SMA, RSI, trend i strefy liczone na pelnej pobranej
historii, dopiero potem wykres jest przycinany. Pokazywane sa tylko strefy AKTYWNE.

Trend (punkty -3..+3): struktura swingow (HH/HL = +1, LH/LL = -1), cena vs SMA 50 (+1 / -1),
nachylenie SMA 50 wzgledem 10 swiec wczesniej (+1 / -1). Wynik >= +2 = WZROSTOWY,
<= -2 = SPADKOWY, w pozostalych przypadkach BOCZNY.

Najsilniejszy opor = poziom powyzej ceny z najwieksza konfluencja (suma wag nakladajacych sie
kandydatow): OB↓ 3, RB↓ 2, SMA 50 (gdy nad cena) 2, FVG↓ 1, szczyt swingowy 1.
Remis rozstrzyga poziom blizszy cenie. To poziom orientacyjny, nie gwarancja zatrzymania ruchu.

Zrodlo glowne: OKX (BTC-USDT spot, swiece UTC: 1Dutc / 1Wutc / 1Mutc), bez kluczy API.
OKX domyslnie otwiera swiece D1/W1/M1 o polnocy UTC+8 (16:00 UTC), dlatego uzywamy
wariantow *utc. Zapas: Kraken XBT/USD (D1, W1; M1 skladane z W1 - przyblizenie).
Zrodlo jest zapisywane tylko w logu. Swieca biezaca (w trakcie) jest pokazywana.

Zmienne srodowiskowe:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  TELEGRAM_THREAD_WYKRESY   - ID tematu #WYKRESY (93)

Uruchomienie lokalne bez wysylki:  python 04_BTC_Charts.py --dry-run
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

import numpy as np
import requests
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_WYKRESY")  # ID tematu #WYKRESY (93)

TZ = ZoneInfo("Europe/Warsaw")
NBSP = "\u00a0"
HTTP_TIMEOUT = 30
OUT_DIR = Path("tmp_charts")

OKX_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_INST = "BTC-USDT"
OKX_PAGE = 100

SMA_PERIOD = 50
RSI_PERIOD = 14
RSI_HIGH, RSI_LOW = 70, 30
LOOKBACK_MONTHS = 12
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
MAX_ZONES = 3           # tyle najnowszych aktywnych stref kazdego typu na wykresie

# Trend i opor
TREND_SWING = 5         # swing do oceny struktury: ekstremum wzgledem 5 swiec z kazdej strony
SMA_SLOPE_BARS = 10     # nachylenie SMA 50: porownanie z wartoscia sprzed 10 swiec
RES_TOL_ATR = 0.5       # poziomy w odleglosci <= 0.5 ATR traktowane jako konfluencja
RES_SWINGS = 5          # ile ostatnich szczytow swingowych bierzemy jako kandydatow
RES_WEIGHTS = {"OB": 3, "RB": 2, "SMA": 2, "FVG": 1, "SWING": 1}

ZONE_STYLE = {          # (kolor, etykieta)
    ("FVG", 1): ("#2ca02c", "FVG↑"), ("FVG", -1): ("#d62728", "FVG↓"),
    ("OB", 1): ("#1f77b4", "OB↑"), ("OB", -1): ("#ff7f0e", "OB↓"),
    ("RB", 1): ("#9467bd", "RB↑"), ("RB", -1): ("#8c564b", "RB↓"),
}

# etykieta -> (bar OKX, interwal Kraken [min], ile swiec potrzeba: okno + zapas)
CHARTS = {
    "D1": ("1Dutc", 1440, 365 + WARMUP),
    "W1": ("1Wutc", 10080, 53 + WARMUP),
    "M1": ("1Mutc", None, LOOKBACK_MONTHS + WARMUP),
}


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


def fetch_okx(bar, need):
    rows, after = [], None
    while len(rows) < need:
        params = {"instId": OKX_INST, "bar": bar, "limit": OKX_PAGE}
        if after:
            params["after"] = after          # rekordy starsze niz ts
        data = http_get(OKX_URL, params)
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
                    {"pair": "XBTUSD", "interval": interval})
    if data.get("error"):
        raise RuntimeError(data["error"])
    result = data["result"]
    key = next(k for k in result if k != "last")
    df = pd.DataFrame(result[key], columns=["t", "Open", "High", "Low", "Close",
                                            "vwap", "Volume", "count"])
    df.index = pd.to_datetime(df["t"], unit="s")
    return df[list(OHLC_COLS)].astype(float).sort_index()


def get_candles(label):
    bar, kraken_interval, need = CHARTS[label]
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
    """Zwraca (etykieta, ikona, opis). Wejscie: pelna historia ze kolumna SMA."""
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


def find_resistance(df, zones):
    """Najsilniejszy opor nad cena (konfluencja wag). Zwraca dict lub None."""
    price = df["Close"].iloc[-1]
    atr = atr_series(df).iloc[-1]
    tol = RES_TOL_ATR * atr if pd.notna(atr) else 0.0

    cands = []  # (dol, gora, waga, nazwa)
    for z in zones:
        if z["dir"] == -1 and z["top"] > price:
            cands.append((z["bottom"], z["top"], RES_WEIGHTS[z["kind"]], f"{z['kind']}↓"))
    s = df["SMA"].iloc[-1]
    if pd.notna(s) and s > price:
        cands.append((s, s, RES_WEIGHTS["SMA"], f"SMA {SMA_PERIOD}"))
    highs, _ = swing_points(df)
    for _, hp in highs[-RES_SWINGS:]:
        if hp > price:
            cands.append((hp, hp, RES_WEIGHTS["SWING"], "szczyt"))
    if not cands:
        return None

    best = None
    for lo, hi, _, _ in cands:
        members = [c for c in cands if c[0] <= hi + tol and c[1] >= lo - tol]
        strength = sum(c[2] for c in members)
        bottom = min(c[0] for c in members)
        top = max(c[1] for c in members)
        names = list(dict.fromkeys(c[3] for c in sorted(members, key=lambda c: -c[2])))
        dist = max(bottom - price, 0.0)
        key = (strength, -dist)
        if best is None or key > best[0]:
            best = (key, {"bottom": bottom, "top": top, "names": names,
                          "dist_pct": dist / price * 100})
    return best[1]


# ---------------------------------------------------------------- wykres

def prepare(df):
    """Wskazniki, trend, opor i strefy na pelnej historii, potem przyciecie do 12 miesiecy."""
    df = df.copy()
    df["SMA"] = df["Close"].rolling(SMA_PERIOD).mean()
    df["RSI"] = rsi_wilder(df["Close"])
    zones = detect_zones(df)
    trend = detect_trend(df)
    resistance = find_resistance(df, zones)
    cut = df.index.get_indexer([df.index[df.index > df.index[-1] - pd.DateOffset(months=LOOKBACK_MONTHS)][0]])[0]
    view = df.iloc[cut:]
    for z in zones:
        z["start"] = max(z["start"] - cut, 0)       # pozycja wzgledem widocznego okna
    return view, zones, trend, resistance


def build_chart(df, zones, resistance, label, path):
    n = len(df)
    ap = []
    if df["SMA"].notna().any():
        ap.append(mpf.make_addplot(df["SMA"], color="#1f77b4", width=1.4))
    ap += [
        mpf.make_addplot(df["RSI"], panel=2, color="#7f3fbf", width=1.2,
                         ylabel=f"RSI {RSI_PERIOD}", ylim=(0, 100)),
        mpf.make_addplot(pd.Series(RSI_HIGH, index=df.index), panel=2, color="gray",
                         linestyle="--", width=0.7),
        mpf.make_addplot(pd.Series(RSI_LOW, index=df.index), panel=2, color="gray",
                         linestyle="--", width=0.7),
    ]
    fills = []
    for z in zones:
        where = np.zeros(n, dtype=bool)
        where[z["start"]:] = True
        fills.append(dict(y1=np.full(n, z["bottom"]), y2=np.full(n, z["top"]),
                          where=where, alpha=0.18, color=ZONE_STYLE[(z["kind"], z["dir"])][0]))

    kwargs = dict(
        type="candle", style="yahoo", volume=True, addplot=ap, returnfig=True,
        title=f"BTC {label} - SMA {SMA_PERIOD}, RSI {RSI_PERIOD}, FVG/OB/RB, opór",
        ylabel="USD(T)", ylabel_lower="Wolumen", figsize=(14, 10), tight_layout=True,
        panel_ratios=(6, 1.5, 2.5),
    )
    if fills:
        kwargs["fill_between"] = fills
    if resistance:
        kwargs["hlines"] = dict(hlines=[resistance["bottom"], resistance["top"]],
                                colors=["#d62728", "#d62728"], linestyle="--", linewidths=1.2)
    fig, axes = mpf.plot(df, **kwargs)

    ax = axes[0]
    for z in zones:
        color, text = ZONE_STYLE[(z["kind"], z["dir"])]
        ax.text(z["start"], z["top"], text, color=color, fontsize=8, fontweight="bold",
                va="bottom", ha="left")
    if resistance:
        ax.text(0, resistance["top"], "OPÓR", color="#d62728", fontsize=9, fontweight="bold",
                va="bottom", ha="left")
    fig.savefig(str(path), dpi=150, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------- formatowanie

def usd(v):
    return "$" + f"{v:,.0f}".replace(",", NBSP)


def build_caption(label, df, zones, trend, resistance, today_iso):
    """Ten sam uklad dla kazdego interwalu (D1 / W1 / M1)."""
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

    if resistance:
        lo, hi = resistance["bottom"], resistance["top"]
        rng = usd(lo) if round(lo) == round(hi) else f"{usd(lo)} - {usd(hi)}"
        res_txt = (f"🧱 <b>Najsilniejszy opór</b>: {rng} ({resistance['dist_pct']:+.1f}%) - "
                   f"{' + '.join(resistance['names'])}")
    else:
        res_txt = "🧱 <b>Najsilniejszy opór</b>: brak poziomów nad ceną"

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
            f"{sma_txt}\n{rsi_txt}\n{trend_txt}\n{res_txt}\n" + "\n".join(lines))


# ---------------------------------------------------------------- telegram

def send_telegram_photo(path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    print(f"Telegram: temat = {TELEGRAM_THREAD_ID or 'BRAK (wiadomość trafi do General)'}")
    with open(path, "rb") as f:
        res = requests.post(url, data=payload, files={"photo": f}, timeout=HTTP_TIMEOUT * 2)
    if not res.ok:
        print(f"Błąd Telegrama: {res.text}")
    res.raise_for_status()


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki na Telegram")
    args = parser.parse_args()

    today_iso = datetime.now(TZ).strftime("%d.%m.%Y")
    OUT_DIR.mkdir(exist_ok=True)

    charts = []
    for label in CHARTS:
        raw, source = get_candles(label)
        print(f"[{label}] źródło danych: {source}")      # tylko log, nie Telegram
        df, zones, trend, resistance = prepare(raw)
        if df.empty:
            sys.exit(f"Brak danych dla {label} - przerywam.")
        path = OUT_DIR / f"BTC_{label}.png"
        build_chart(df, zones, resistance, label, path)
        caption = build_caption(label, df, zones, trend, resistance, today_iso)
        print(caption)
        charts.append((path, caption))

    if args.dry_run:
        print(f"Dry-run: wykresy zapisane w {OUT_DIR}/, bez wysyłki.")
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        sys.exit("Brak TELEGRAM_TOKEN / TELEGRAM_CHAT_ID.")
    for path, caption in charts:                          # kazdy wykres osobno
        send_telegram_photo(path, caption)
        time.sleep(1)


if __name__ == "__main__":
    main()
