"""
03_BTC_Signal.py

Dzienny sygnał dla BTC wysyłany na Telegram. Zasady z promptu (10 wskaźników, wagi,
mnożniki M = +1 / 0 / -1, alokacja LONG/SHORT, transze P1-P3, filtr anty-FOMO) zostały
zamienione na deterministyczny kod, więc wynik jest powtarzalny.

Źródła danych (darmowe, bez kluczy):
  - Kraken            : świece D1 i W1, cena spot
  - CoinMetrics       : kapitalizacja rynkowa i zrealizowana -> MVRV Z-Score
  - Hyperliquid       : funding rate i Open Interest BTC
  - alternative.me    : Fear and Greed Index (dodatek)

Gdy któreś źródło nie odpowie, wskaźnik dostaje status N/D i jest pomijany, a wagi
pozostałych są przeskalowane do 100%. W komunikacie widać pokrycie danych (np. 9/10).

Przybliżenia (zaznaczone w komunikacie):
  - STH Realized Price = VWAP z 155 dni (proxy; prawdziwa wartość wymaga danych UTXO),
  - Puell Multiple = przychód z emisji (nagroda za blok x cena) / średnia 365 dni.
    Uwaga: oba proxy wynikają wyłącznie z ceny, więc NIE wnoszą niezależnej informacji
    on-chain (dlatego mają umiarkowane wagi).

Przegląd reguł i wag (backtest kodu produkcyjnego na dziennych danych Bitstamp 2016-2024,
tylko wskaźniki cenowe; MVRV, funding/OI i Fear/Greed nie mają darmowej historii):
  - cena poniżej 200W SMA NIE poprzedzała spadków (śr. +8% w 30 dni) -> M = 0, nie -1,
  - RSI D1 powyżej 75 poprzedzał wzrosty (śr. +15% w 30 dni) -> M = 0, nie -1
    (zostaje tylko jako filtr anty-FOMO przy wejściu),
  - Pi Cycle Top był najsilniejszym sygnałem szczytu (śr. -18% w 30 dni) -> wyższa waga
    i blokada nowych LONG, gdy aktywny,
  - noga SHORT miała w próbie ujemny wynik (zysk tylko w bessie 2022) -> SHORT x0.5,
  - sizing "tylko strona dominująca" podwajał ekspozycję netto względem wzoru
    (100 +/- wynik) / 2 i pogłębiał obsunięcie -> wielkość liczona netto,
  - stop 4-5% (~1 ATR) był wybijany w ok. 55-62% wejść w ciągu 10 dni -> SL = 2 x ATR,
    a wielkość pozycji ograniczona budżetem ryzyka.
Wagi nie są "zoptymalizowane" - próba obejmuje ok. 2,5 cyklu BTC, więc różnice między
zestawami wag mieszczą się w szumie. Traktuj wynik jako uporządkowany przegląd wskaźników,
a nie prognozę kursu.

Stan (historia sygnałów i Open Interest do oceny trendu OI) zapisuje się w
03_BTC_History.json - workflow powinien go zatwierdzać do repozytorium.

Uruchomienie lokalne bez wysyłki:  python 03_BTC_Signal.py --dry-run
"""

import os
import sys
import json
import math
import time
import argparse
import statistics
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_SIGNAL")  # ID tematu #3 BTC_Signal (8)

STATE_FILE = "03_BTC_History.json"
STATE_DAYS = 400
TZ = ZoneInfo("Europe/Warsaw")
NBSP = "\u00a0"
HTTP_TIMEOUT = 30

# ---------------------------------------------------------------- parametry

# Wagi rdzenia (suma = 100). Grupy: trend/struktura 50, wycena on-chain 30,
# pochodne 10, szczyt cyklu 7, krótki termin 3.
WEIGHTS = {
    "sma200w": 10,
    "sma200d": 18,
    "mvrv": 15,
    "rsi_w1": 10,
    "bb": 12,
    "sth": 12,
    "funding": 10,
    "rsi_d1": 3,
    "puell": 3,
    "pi": 7,
}

NAMES = {
    "sma200w": "200W SMA",
    "sma200d": "200D SMA",
    "mvrv": "MVRV Z-Score",
    "rsi_w1": "RSI W1",
    "bb": "Bollinger W1+D1",
    "sth": "STH Realized Price (proxy)",
    "funding": "Funding + OI",
    "rsi_d1": "RSI D1",
    "puell": "Puell Multiple (proxy)",
    "pi": "Pi Cycle Top",
}

NEUTRAL_BAND = 10      # |wynik netto| ponizej tej wartosci = brak nowych pozycji
STRONG_LEVEL = 50      # |wynik netto| od tej wartosci = sygnal "MOCNY"
SMA_NEUTRAL_PCT = 3.0  # odchylenie od SMA uznawane za konsolidację

TRANCHES = [(0.50, 1), (0.30, 3), (0.20, 5)]  # (udzial alokacji, dzwignia)
SL_ATR_MULT = 2.0      # stop-loss = 2 x ATR(14) dzienny (wczesniej sztywne 4-5% ~ 1 ATR)
SL_MIN, SL_MAX = 6.0, 12.0   # granice stop-lossu w % od wejscia transzy
TP1_R = 1.5            # TP1 = 1.5 x odleglosc SL od P1
ATR_STEP = 1.0         # P2 = P1 +/- 1 ATR, P3 = P1 +/- 2 ATR (po wypelnieniu P1)

SHORT_SIZE_SCALE = 0.5   # noga SHORT historycznie bez przewagi -> polowa wielkosci
RISK_BUDGET_PCT = 5.0    # maks. strata przy SL wszystkich transz (% kapitalu)

RSI_FOMO_LONG = 75     # zakaz LONG po rynkowej, gdy RSI D1 powyzej
RSI_FOMO_SHORT = 30    # zakaz SHORT po rynkowej, gdy RSI D1 ponizej

# Dodatki (korekta wyniku, lacznie max +-10 pkt)
EXTRA_FEAR_GREED_PTS = 4
EXTRA_MA_CROSS_PTS = 3
EXTRA_MOMENTUM_PTS = 3

HALVINGS = [date(2012, 11, 28), date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 20)]
# po kolejnym halvingu (ok. 2028) dopisz jego date do listy


# ---------------------------------------------------------------- HTTP / dane

def http_get(url, params=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                             headers={"User-Agent": "btc-signal-bot"})
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


def fetch_kraken_ohlc(interval):
    data = http_get("https://api.kraken.com/0/public/OHLC",
                    {"pair": "XBTUSD", "interval": interval})
    if data.get("error"):
        raise RuntimeError(data["error"])
    result = data["result"]
    key = next(k for k in result if k != "last")
    candles = [{
        "t": int(c[0]), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]),
        "c": float(c[4]), "vwap": float(c[5]), "v": float(c[6]),
    } for c in result[key]]
    return candles[:-1]  # ostatnia swieca jest niedomknieta


def fetch_spot():
    data = http_get("https://api.kraken.com/0/public/Ticker", {"pair": "XBTUSD"})
    key = next(iter(data["result"]))
    return float(data["result"][key]["c"][0])


def fetch_mvrv_z():
    """
    MVRV Z-Score = (kapitalizacja rynkowa - kapitalizacja zrealizowana) / odch. std. kapitalizacji.
    Dwa warianty zapytania do CoinMetrics (jeśli darmowe API nie udostępnia CapRealUSD,
    kapitalizacja zrealizowana jest wyliczana z CapMVRVCur).
    """
    url = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
    errors = []
    for metrics in ("CapMrktCurUSD,CapRealUSD", "CapMrktCurUSD,CapMVRVCur"):
        try:
            data = http_get(url, {"assets": "btc", "metrics": metrics, "frequency": "1d",
                                  "start_time": "2010-07-18", "page_size": 10000})
            second = metrics.split(",")[1]
            rows = [r for r in data["data"] if r.get("CapMrktCurUSD") and r.get(second)]
            caps = [float(r["CapMrktCurUSD"]) for r in rows]
            market_cap = caps[-1]
            if second == "CapRealUSD":
                realized_cap = float(rows[-1]["CapRealUSD"])
            else:
                realized_cap = market_cap / float(rows[-1]["CapMVRVCur"])
            print(f"MVRV: źródło {metrics}")
            return (market_cap - realized_cap) / statistics.pstdev(caps)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{metrics}: {e}")
    raise RuntimeError(" | ".join(errors))


def fetch_hyperliquid_btc():
    res = requests.post("https://api.hyperliquid.xyz/info",
                        json={"type": "metaAndAssetCtxs"}, timeout=HTTP_TIMEOUT)
    res.raise_for_status()
    meta, ctxs = res.json()
    idx = next(i for i, a in enumerate(meta["universe"]) if a["name"] == "BTC")
    ctx = ctxs[idx]
    mark = float(ctx["markPx"])
    return {
        # Hyperliquid placi funding co godzine -> przeliczenie na ekwiwalent 8h (w %)
        "funding_8h_pct": float(ctx["funding"]) * 8 * 100,
        "oi_usd": float(ctx["openInterest"]) * mark,
    }


def fetch_fear_greed():
    data = http_get("https://api.alternative.me/fng/", {"limit": 1})
    item = data["data"][0]
    return int(item["value"]), item["value_classification"]


def safe(label, fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        print(f"[BLAD] {label}: {e}")
        return None


# ---------------------------------------------------------------- wskazniki

def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def bollinger(closes, n=20, k=2.0, lookback=100):
    def bands(window):
        mid = sum(window) / n
        sd = statistics.pstdev(window)
        return mid - k * sd, mid, mid + k * sd

    lower, mid, upper = bands(closes[-n:])
    bws = []
    for end in range(len(closes) - lookback + 1, len(closes) + 1):
        lo, mi, up = bands(closes[end - n:end])
        bws.append((up - lo) / mi)
    bw_now = bws[-1]
    squeeze = bw_now <= sorted(bws)[int(0.15 * len(bws))]
    expanding = bw_now > bws[-4]
    return {"lower": lower, "mid": mid, "upper": upper, "squeeze": squeeze, "expanding": expanding}


def swing_points(candles, k=5):
    highs, lows = [], []
    for i in range(k, len(candles) - k):
        window = range(i - k, i + k + 1)
        if all(candles[i]["h"] > candles[j]["h"] for j in window if j != i):
            highs.append(candles[i]["h"])
        if all(candles[i]["l"] < candles[j]["l"] for j in window if j != i):
            lows.append(candles[i]["l"])
    return highs, lows


def market_structure(daily):
    highs, lows = swing_points(daily[-250:])
    if len(highs) < 2 or len(lows) < 2:
        return "MIX"
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    if hh and hl:
        return "HH/HL"
    if not hh and not hl:
        return "LH/LL"
    return "MIX"


def block_subsidy(d):
    return 50 / (2 ** sum(1 for h in HALVINGS if d >= h))


def puell_multiple(daily):
    last = daily[-365:]
    issuance = [
        block_subsidy(datetime.fromtimestamp(c["t"], timezone.utc).date()) * 144 * c["c"]
        for c in last
    ]
    return issuance[-1] / (sum(issuance) / len(issuance))


def sth_proxy(daily, days=155):
    last = daily[-days:]
    volume = sum(c["v"] for c in last)
    return sum(c["vwap"] * c["v"] for c in last) / volume if volume > 0 else None


def atr(candles, n=14):
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


# ---------------------------------------------------------------- formatowanie

def usd(v):
    return "$" + f"{v:,.0f}".replace(",", NBSP)


def pct(v, d=1):
    return f"{v:+.{d}f}%"


def label_of(m):
    if m is None:
        return "N/D"
    if m > 0.25:
        return "WZROST"
    if m < -0.25:
        return "SPADEK"
    return "KONSOLIDACJA"


def icon_of(m):
    if m is None:
        return "⚪️"
    return "🟢" if m > 0.25 else ("🔴" if m < -0.25 else "🟡")


def round5(x):
    return int(math.floor(x / 5 + 0.5) * 5)


# ---------------------------------------------------------------- ocena wskaznikow

def evaluate_indicators(spot, daily, weekly, mvrv_z, hl, oi_trend):
    d_close = [c["c"] for c in daily]
    w_close = [c["c"] for c in weekly]
    out = {}

    # 1. 200W SMA (powyzej = WZROST; ponizej = strefa wartosci, NIE sygnal spadku)
    s = sma(w_close, 200)
    if s:
        dev = (spot / s - 1) * 100
        m = 1 if dev > SMA_NEUTRAL_PCT else 0
        note = " (strefa wartości)" if dev < -SMA_NEUTRAL_PCT else ""
        out["sma200w"] = (m, "200W SMA", f"cena {pct(dev)} vs {usd(s)}{note}")

    # 2. 200D SMA + struktura rynku
    s = sma(d_close, 200)
    if s:
        dev = (spot / s - 1) * 100
        struct = market_structure(daily)
        if abs(dev) < SMA_NEUTRAL_PCT:
            m = 0
        elif dev > 0 and struct == "HH/HL":
            m = 1
        elif dev < 0 and struct == "LH/LL":
            m = -1
        else:
            m = 0
        out["sma200d"] = (m, "200D SMA", f"{struct}, cena {pct(dev)} vs {usd(s)}")

    # 3. MVRV Z-Score
    if mvrv_z is not None:
        z = mvrv_z
        if z < 0:
            m, why = 1, "kapitulacja (sygnał kontrariański)"
        elif z < 1:
            m, why = 0, "strefa przejściowa"
        elif z <= 6:
            m, why = 1, "zdrowa hossa"
        elif z < 6.5:
            m, why = 0, "wysoka wycena, jeszcze bez przegrzania"
        else:
            m, why = -1, "przegrzanie"
        out["mvrv"] = (m, "MVRV Z-Score", f"{z:.2f} - {why}")

    # 4. RSI W1
    r = rsi(w_close)
    if r is not None:
        m = 1 if r > 50 else (0 if r >= 45 else -1)
        out["rsi_w1"] = (m, "RSI W1", f"{r:.1f}")

    # 5. Wstegi Bollingera W1 + D1
    if len(w_close) >= 125 and len(d_close) >= 125:
        parts, texts = [], []
        for name, closes in (("W1", w_close), ("D1", d_close)):
            b = bollinger(closes)
            if spot > b["upper"] and b["expanding"]:
                parts.append(1)
                texts.append(f"{name} wybicie w górę")
            elif spot < b["lower"] and b["expanding"]:
                parts.append(-1)
                texts.append(f"{name} wybicie w dół")
            elif b["squeeze"]:
                parts.append(0)
                texts.append(f"{name} squeeze")
            else:
                parts.append(0)
                texts.append(f"{name} w kanale")
        out["bb"] = (sum(parts) / 2, "Bollinger W1+D1", ", ".join(texts))

    # 6. STH Realized Price (proxy)
    p = sth_proxy(daily)
    if p:
        dev = (spot / p - 1) * 100
        out["sth"] = (1 if spot > p else -1, "STH Realized Price (proxy)",
                      f"cena {pct(dev)} vs {usd(p)}")

    # 7. Funding Rate + Open Interest (z kontekstem kierunku ceny)
    if hl:
        f = hl["funding_8h_pct"]
        chg3 = (spot / d_close[-4] - 1) * 100
        support_break = spot < min(c["l"] for c in daily[-20:])
        if f > 0.05 and chg3 < 0:
            m, why = -1, "skrajnie wysoki funding przy spadku ceny"
        elif f < -0.02 and support_break:
            m, why = -1, "skrajnie ujemny funding przy wybiciu wsparcia"
        elif oi_trend == "up" and chg3 < -1.0:
            m, why = -1, "rosnące OI przy spadku ceny (nowe shorty)"
        elif 0.005 <= f <= 0.03 and oi_trend == "up" and chg3 > 0:
            m, why = 1, "lekko dodatni funding, rosnące OI i cena"
        elif abs(f) < 0.005 and oi_trend in ("flat", None):
            m, why = 0, "funding ~0, płaskie OI"
        else:
            m, why = 0, "brak wyraźnego sygnału"
        oi_txt = {"up": "OI rośnie", "down": "OI spada", "flat": "OI płaskie", None: "OI bez historii"}[oi_trend]
        out["funding"] = (m, "Funding + OI", f"{f:.4f}%/8h, {oi_txt} - {why}")

    # 8. RSI D1 (wyprzedanie = WZROST; wykupienie tylko jako filtr wejscia, nie sygnal spadku)
    r = rsi(d_close)
    if r is not None:
        if r < 30:
            m, why = 1, "wyprzedanie"
        elif r > 75:
            m, why = 0, "wykupienie (filtr wejścia, nie sygnał spadku)"
        else:
            m, why = 0, "neutralnie"
        out["rsi_d1"] = (m, "RSI D1", f"{r:.1f} - {why}")

    # 9. Puell Multiple (proxy)
    if len(daily) >= 365:
        pm = puell_multiple(daily)
        m = 1 if pm < 0.6 else (-1 if pm > 4.0 else 0)
        out["puell"] = (m, "Puell Multiple (proxy)", f"{pm:.2f}")

    # 10. Pi Cycle Top
    s111, s350 = sma(d_close, 111), sma(d_close, 350)
    if s111 and s350:
        active = s111 > 2 * s350
        out["pi"] = (-1 if active else 0, "Pi Cycle Top",
                     "sygnał szczytu AKTYWNY" if active else "brak sygnału szczytu")

    return out


def evaluate_extras(spot, daily, fear_greed):
    d_close = [c["c"] for c in daily]
    extras = []

    if fear_greed:
        v, cls = fear_greed
        pts = EXTRA_FEAR_GREED_PTS if v <= 20 else (-EXTRA_FEAR_GREED_PTS if v >= 80 else 0)
        extras.append(("Fear/Greed", pts, f"{v} ({cls})"))

    s50, s200 = sma(d_close, 50), sma(d_close, 200)
    if s50 and s200:
        pts = EXTRA_MA_CROSS_PTS if s50 > s200 else -EXTRA_MA_CROSS_PTS
        extras.append(("50D vs 200D SMA", pts,
                       "SMA50 powyżej SMA200" if s50 > s200 else "SMA50 poniżej SMA200"))

    if len(d_close) > 90:
        mom = (spot / d_close[-90] - 1) * 100
        pts = EXTRA_MOMENTUM_PTS if mom > 10 else (-EXTRA_MOMENTUM_PTS if mom < -10 else 0)
        extras.append(("Momentum 90D", pts, pct(mom)))

    return extras


def compute_score(indicators, extras):
    available = {k: v for k, v in indicators.items() if v[0] is not None}
    total_w = sum(WEIGHTS[k] for k in available)
    if total_w == 0:
        return None
    core = sum(WEIGHTS[k] * available[k][0] for k in available) / total_w * 100
    overlay = sum(p for _, p, _ in extras)
    final = max(-100.0, min(100.0, core + overlay))

    stage = {"WZROST": 0.0, "KONSOLIDACJA": 0.0, "SPADEK": 0.0}
    for k, v in available.items():
        stage[label_of(v[0])] += WEIGHTS[k] / total_w * 100
    return {"core": core, "overlay": overlay, "final": final,
            "stage": stage, "coverage": (len(available), len(WEIGHTS))}


def signal_of(n):
    if n >= STRONG_LEVEL:
        return "MOCNY LONG", "🟢🟢"
    if n >= NEUTRAL_BAND:
        return "LONG", "🟢"
    if n > -NEUTRAL_BAND:
        return "NEUTRALNIE - brak nowych pozycji", "🟡"
    if n > -STRONG_LEVEL:
        return "SHORT", "🔴"
    return "MOCNY SHORT", "🔴🔴"


# ---------------------------------------------------------------- plan pozycji

def build_plan(final_score, spot, daily):
    # rozklad kierunkowy wg wzoru z promptu (po zaokragleniu do 5%)
    long_alloc = round5((100 + final_score) / 2)
    short_alloc = 100 - long_alloc

    if final_score >= NEUTRAL_BAND:
        direction = 1
    elif final_score <= -NEUTRAL_BAND:
        direction = -1
    else:
        direction = 0

    plan = {"long_alloc": long_alloc, "short_alloc": short_alloc,
            "direction": direction, "open_alloc": 0.0}
    if direction == 0:
        return plan

    d_close = [c["c"] for c in daily]
    a = atr(daily)
    rsi_d1 = rsi(d_close)
    sma20 = sma(d_close, 20)
    sl_pct = max(SL_MIN, min(SL_MAX, SL_ATR_MULT * a / spot * 100))

    # P1: rynkowo albo - przy filtrze anty-FOMO - limit na retescie sredniej 20D
    # (ograniczony do 0.5-1.5 ATR od ceny)
    p1, p1_limit = spot, False
    if direction == 1 and rsi_d1 is not None and rsi_d1 > RSI_FOMO_LONG:
        p1 = min(spot - 0.5 * a, max(sma20, spot - 1.5 * a))
        p1_limit = True
    elif direction == -1 and rsi_d1 is not None and rsi_d1 < RSI_FOMO_SHORT:
        p1 = max(spot + 0.5 * a, min(sma20, spot + 1.5 * a))
        p1_limit = True

    levels = [p1, p1 + direction * ATR_STEP * a, p1 + direction * 2 * ATR_STEP * a]
    eff_lev = sum(share * lev for share, lev in TRANCHES)

    # wielkosc: ekspozycja NETTO wg wzoru (= |wynik|), SHORT x SHORT_SIZE_SCALE,
    # ograniczona budzetem ryzyka przy stop-lossie wszystkich transz
    target = round5(abs(final_score)) * (SHORT_SIZE_SCALE if direction == -1 else 1.0)
    risk_cap = RISK_BUDGET_PCT / (eff_lev * sl_pct / 100)
    open_alloc = math.floor(min(target, risk_cap) * 2) / 2
    if open_alloc <= 0:
        plan["direction"] = 0
        return plan

    plan.update({
        "levels": levels,
        "p1_limit": p1_limit,
        "sl_pct": sl_pct,
        "tp1": p1 + direction * TP1_R * sl_pct / 100 * p1,
        "open_alloc": open_alloc,
        "target": target,
        "risk_cap": risk_cap,
        "max_loss_pct": open_alloc * eff_lev * sl_pct / 100,
        "rsi_d1": rsi_d1,
    })
    return plan


def weekly_range(daily, spot):
    closes = [c["c"] for c in daily[-31:]]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    sigma = statistics.pstdev(rets) * math.sqrt(7)
    return spot * math.exp(-sigma), spot * math.exp(sigma)


# ---------------------------------------------------------------- stan

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Nie udało się wczytać stanu: {e}")
        return {}


def save_state(state):
    for d in sorted(state)[:-STATE_DAYS]:
        del state[d]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def previous_entry(state, today_iso):
    older = sorted(d for d in state if d < today_iso)
    return (older[-1], state[older[-1]]) if older else (None, None)


def fmt_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


# ---------------------------------------------------------------- komunikat

def build_message(today_iso, spot, daily, indicators, extras, score, plan, prev_date, prev):
    n = score["final"]
    sig_text, sig_icon = signal_of(n)
    chg7 = (spot / daily[-7]["c"] - 1) * 100
    lo, hi = weekly_range(daily, spot)

    msg = f"<b>₿ BTC - sygnał dnia</b> {fmt_date(today_iso)}\n"
    msg += f"Cena: <b>{usd(spot)}</b> (7D: {pct(chg7)}) | (zmienność 7D: {usd(lo)} - {usd(hi)})\n\n"

    msg += "<b>Wskaźniki (waga):</b>\n"
    for key in WEIGHTS:
        if key in indicators:
            m, name, why = indicators[key]
            msg += (f"{icon_of(m)} <b>{name}</b>: {label_of(m)}: {why} "
                    f"(waga {WEIGHTS[key]}%)\n")
        else:
            msg += f"⚪️ <b>{NAMES[key]}</b>: N/D - brak danych (waga {WEIGHTS[key]}%)\n"

    if extras:
        msg += "\n<b>Dodatki (korekta wyniku):</b>\n"
        for name, pts, why in extras:
            ic = "🟢" if pts > 0 else ("🔴" if pts < 0 else "🟡")
            msg += f"{ic} <b>{name}</b> {pts:+d} pkt: {why}\n"

    st = score["stage"]
    dominant = max(st, key=st.get)
    msg += (f"\n<b>Etap rynku:</b> {dominant} "
            f"(wzrost {st['WZROST']:.0f}% / konsolidacja {st['KONSOLIDACJA']:.0f}% / "
            f"spadek {st['SPADEK']:.0f}% wag)\n")
    msg += (f"<b>Wynik netto:</b> {n:+.0f} pkt (rdzeń {score['core']:+.0f}, "
            f"dodatki {score['overlay']:+d}) | dane: {score['coverage'][0]}/{score['coverage'][1]}\n\n")

    d = plan["direction"]
    open_alloc = plan["open_alloc"]
    short_status = "AKTYWNY" if d == -1 else "WSTRZYMANY"
    long_status = "AKTYWNY" if d == 1 else "WSTRZYMANY"
    short_open = open_alloc if d == -1 else 0
    long_open = open_alloc if d == 1 else 0
    msg += (f"<b>Pozycja SHORT:</b> {plan['short_alloc']}% kapitału "
            f"(do otwarcia: {short_open:g}%) - <b>{short_status}</b>\n")
    msg += (f"<b>Pozycja LONG:</b> {plan['long_alloc']}% kapitału "
            f"(do otwarcia: {long_open:g}%) - <b>{long_status}</b>\n")

    if d != 0:
        side = "LONG" if d == 1 else "SHORT"
        p1, p2, p3 = plan["levels"]
        a = plan["open_alloc"]
        p1_txt = f"{usd(p1)} (limit, retest)" if plan["p1_limit"] else f"{usd(p1)} (rynkowo)"

        size_txt = f"|wynik| {round5(abs(n)):g}%"
        if d == -1:
            size_txt += f" x{SHORT_SIZE_SCALE:g} (SHORT)"
        size_txt += f" = {plan['target']:g}%"
        if plan["target"] > plan["risk_cap"]:
            size_txt += f", ograniczone budżetem ryzyka do {open_alloc:g}%"

        msg += (
            f"\n<b>Plan {side} (margin {open_alloc:g}% kapitału):</b>\n"
            f"<i>Wielkość: {size_txt}</i>\n"
            f"P1 (50%, x1): {p1_txt} - {a * 0.5:.1f}% kapitału\n"
            f"P2 (30%, x3): {usd(p2)} - {a * 0.3:.1f}% kapitału\n"
            f"P3 (20%, x5): {usd(p3)} - {a * 0.2:.1f}% kapitału\n"
            f"P2/P3 tylko po wypełnieniu P1 (zlecenia stop zgodne z trendem)\n"
            f"SL: {plan['sl_pct']:.1f}% od wejścia każdej transzy ({SL_ATR_MULT:g} x ATR) | "
            f"TP1: {usd(plan['tp1'])}, potem SL na break-even\n"
            f"Maks. strata przy SL ≈ {plan['max_loss_pct']:.1f}% kapitału (gdy wejdą wszystkie transze)\n"
        )
        if plan["p1_limit"]:
            msg += "Filtr anty-FOMO: P1 tylko na retest, bez wejścia po cenie rynkowej.\n"

    if score.get("long_blocked"):
        msg += "\n⚠️ Pi Cycle Top aktywny - blokada nowych pozycji LONG.\n"

    msg += f"\n<b>{sig_icon} SYGNAŁ KOŃCOWY: {sig_text}</b>"

    if prev:
        prev_chg = (spot / prev["price"] - 1) * 100
        prev_sig = signal_of(prev["score"])[0].split(" - ")[0]
        msg += f"\n<i>Poprzedni sygnał ({fmt_date(prev_date)}): {prev_sig}, cena od tego czasu {pct(prev_chg)}</i>"
    return msg


def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    print(f"Telegram: temat = {TELEGRAM_THREAD_ID or 'BRAK (wiadomość trafi do General)'}")
    res = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    if not res.ok:
        print(f"Błąd Telegrama: {res.text}")
    res.raise_for_status()


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki i bez zapisu stanu")
    args = parser.parse_args()

    daily = safe("Kraken D1", fetch_kraken_ohlc, 1440)
    weekly = safe("Kraken W1", fetch_kraken_ohlc, 10080)
    spot = safe("Kraken ticker", fetch_spot)
    if not daily or not weekly or spot is None or len(daily) < 400 or len(weekly) < 210:
        sys.exit("Brak wystarczających danych cenowych z Kraken - przerywam.")

    mvrv_z = safe("MVRV (CoinMetrics)", fetch_mvrv_z)
    hl = safe("Funding/OI (Hyperliquid)", fetch_hyperliquid_btc)
    fear_greed = safe("Fear and Greed", fetch_fear_greed)

    today_iso = datetime.now(TZ).strftime("%Y-%m-%d")
    state = load_state()
    prev_date, prev = previous_entry(state, today_iso)

    oi_trend = None
    if hl and prev and prev.get("oi_usd"):
        ratio = hl["oi_usd"] / prev["oi_usd"] - 1
        oi_trend = "up" if ratio > 0.02 else ("down" if ratio < -0.02 else "flat")

    indicators = evaluate_indicators(spot, daily, weekly, mvrv_z, hl, oi_trend)
    extras = evaluate_extras(spot, daily, fear_greed)
    score = compute_score(indicators, extras)
    if score is None:
        sys.exit("Żaden wskaźnik nie zwrócił wyniku - przerywam.")

    pi = indicators.get("pi")
    if pi and pi[0] == -1 and score["final"] >= NEUTRAL_BAND:
        score["final"] = NEUTRAL_BAND - 1      # Pi Cycle Top aktywny: bez nowych LONG
        score["long_blocked"] = True

    plan = build_plan(score["final"], spot, daily)
    message = build_message(today_iso, spot, daily, indicators, extras, score, plan, prev_date, prev)
    print(message)

    if args.dry_run:
        return

    send_telegram_message(message)
    state[today_iso] = {
        "price": spot,
        "score": round(score["final"], 2),
        "signal": signal_of(score["final"])[0],
        "oi_usd": hl["oi_usd"] if hl else None,
    }
    save_state(state)


if __name__ == "__main__":
    main()
