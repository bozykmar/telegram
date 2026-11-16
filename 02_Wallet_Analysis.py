"""
02_Wallet_Analysis.py

Analiza pozycji portfeli z 01_Wallet_Address.txt i propozycja max 3 pozycji
(waluta, kierunek, wejście, dźwignia, stop-loss) wysyłana jako OSOBNY komunikat
na Telegram.

Skrypt jest SAMODZIELNY: nie importuje 01_Wallet_Whale.py i można go uruchamiać
niezależnie od niego. Potrzebuje tylko 01_Wallet_Address.txt. Własną historię pozycji
(do oceny trendu) zapisuje w 02_Analysis_History.json.

Uruchomienie lokalne bez wysyłki i bez zapisu:  python 02_Wallet_Analysis.py --dry-run

Metodyka (heurystyka konsensusu, nie model predykcyjny):
1. Waga portfela = log10(Account Equity) x (1 + ROE otwartych pozycji, ucięte do +-50%).
2. Przekonanie pozycji = waga portfela x udział marginu pozycji w Account Equity
   x współczynnik trendu (zwiększanie pozycji od poprzedniego dnia +15%, redukcja -15%).
3. Dla każdej waluty sumujemy przekonanie po stronie LONG i SHORT.
   Kandydat: min. MIN_WALLETS portfeli po tej samej stronie i zgodność >= MIN_AGREEMENT.
4. Wejście: cena rynkowa, jeśli mieści się w strefie średniego wejścia portfeli
   (+-ENTRY_TOLERANCE), w przeciwnym razie zlecenie limit na krawędzi strefy.
5. Dźwignia: mediana ważona dźwigni portfeli, ucięta do MAX_LEVERAGE.
6. Stop-loss: poziom, przy którym strata = MAX_LOSS_ON_MARGIN marginu.
7. Wielkość pozycji (% kapitału wpłaconego jako margin):
   RISK_PER_TRADE / MAX_LOSS_ON_MARGIN x zgodność portfeli, ucięte do MAX_MARGIN_PCT.
   Wartość nominalna pozycji = margin x dźwignia.
"""

import os
import re
import json
import math
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_ANALYSIS")  # ID tematu (opcjonalnie)

API_URL = "https://api.hyperliquid.xyz/info"
HEADERS = {"Content-Type": "application/json"}

HISTORY_FILE = "02_Analysis_History.json"
HISTORY_DAYS = 30
TZ = ZoneInfo("Europe/Warsaw")
NBSP = "\u00a0"

# ---------------------------------------------------------------- parametry

MAX_PICKS = 3             # maksymalna liczba propozycji
MIN_WALLETS = 2           # min. liczba portfeli po tej samej stronie
MIN_AGREEMENT = 0.70      # min. zgodność (udział wagi dominującej strony)
MIN_EQUITY = 50_000       # portfele poniżej tej wartości są pomijane (USD)
MAX_LEVERAGE = 5          # górny limit proponowanej dźwigni
MAX_LOSS_ON_MARGIN = 0.15 # stop-loss: strata maks. 15% marginu pozycji
ENTRY_TOLERANCE = 0.03    # strefa wejścia: +-3% od średniego wejścia portfeli
RISK_PER_TRADE = 0.02     # ryzyko na jedną pozycję: 2% kapitału (przy trafieniu w stop-loss)
MAX_MARGIN_PCT = 0.20     # maks. margin jednej pozycji: 20% kapitału


# ---------------------------------------------------------------- narzędzia

def fmt_px(value):
    """Cena: więcej miejsc po przecinku dla tanich aktywów."""
    if value >= 1000:
        return fmt_usd(value, 0)
    decimals = 6 if value < 0.1 else 4
    return f"${value:.{decimals}f}"


def parse_leverage(text):
    m = re.search(r"(\d+(?:\.\d+)?)x", text or "")
    return float(m.group(1)) if m else 1.0


def weighted_median(pairs):
    """pairs: lista (wartość, waga)."""
    pairs = sorted(pairs)
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def wallet_weight(equity, positions):
    size_factor = math.log10(max(equity, 10))
    total_margin = sum(p["margin"] for p in positions)
    roe = sum(p["upnl"] for p in positions) / total_margin if total_margin > 0 else 0.0
    perf = 1 + max(-0.5, min(0.5, roe))
    return size_factor * perf


def trend_factor(prev_positions, p):
    """Porównanie wielkości pozycji (w jednostkach monety) z poprzednim dniem."""
    prev = prev_positions.get(p["coin"])
    if (
        not prev
        or prev.get("direction") != p["direction"]
        or not prev.get("mark_px")
        or not p["mark_px"]
    ):
        return 1.0, "new"
    size_prev = prev["val"] / prev["mark_px"]
    size_now = p["val"] / p["mark_px"]
    if size_prev <= 0:
        return 1.0, "new"
    change = size_now / size_prev - 1
    if change > 0.05:
        return 1.15, "up"
    if change < -0.05:
        return 0.85, "down"
    return 1.0, "flat"


# ---------------------------------------------------------------- formatowanie

def fmt_usd(value, decimals, signed=False):
    sign = ""
    if signed:
        sign = "+" if value >= 0 else "-"
    elif value < 0:
        sign = "-"
    abs_val = abs(value)
    if abs_val >= 1000:
        number = f"{abs_val:,.0f}".replace(",", NBSP)
    else:
        number = f"{abs_val:.{decimals}f}"
    return f"${sign}{number}"


def fmt_date(iso_date):
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")


# ---------------------------------------------------------------- historia (własna)

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Nie udało się wczytać historii: {e}")
        return {}


def save_history(history):
    cutoff = sorted({d for w in history.values() for d in w})[-HISTORY_DAYS:]
    for address in history:
        history[address] = {d: s for d, s in history[address].items() if d in cutoff}
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_previous_snapshot(history, address, today_iso):
    """Zwraca (data, pozycje) z ostatniego dnia wcześniejszego niż dzisiaj."""
    days = history.get(address, {})
    older = sorted(d for d in days if d < today_iso)
    if not older:
        return None, None
    return older[-1], days[older[-1]]


# ---------------------------------------------------------------- konfiguracja

def load_wallet_configs(file_path="01_Wallet_Address.txt"):
    """
    Format linii: Nazwa - Opis - Link
    Przykład: Wallet #1 - najlepszy portfel - https://hyperbot.network/trader/0x...
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Nie znaleziono pliku {file_path}")

    wallets = []
    with open(file_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    for line in lines:
        parts = [p.strip() for p in line.split(" - ") if p.strip()]
        if len(parts) < 2:
            continue
        link = parts[-1]
        if "/trader/" not in link:
            continue
        address = link.split("/trader/")[-1].strip().split("?")[0].strip("/")
        if address:
            wallets.append({"name": parts[0], "address": address})
    return wallets


# ---------------------------------------------------------------- Hyperliquid

def get_hyperliquid_data(address):
    """Zwraca (account_equity, lista_pozycji) lub None przy błędzie."""
    positions = []
    try:
        payload = {"type": "clearinghouseState", "user": address}
        res = requests.post(API_URL, json=payload, headers=HEADERS, timeout=30)
        res.raise_for_status()
        res_perp = res.json()

        account_equity = float(res_perp.get("marginSummary", {}).get("accountValue", 0))

        for item in res_perp.get("assetPositions", []):
            pos = item.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi == 0:
                continue

            position_value = float(pos.get("positionValue", 0))
            upnl = float(pos.get("unrealizedPnl", 0))

            leverage_obj = pos.get("leverage", {})
            lev_type = leverage_obj.get("type", "").capitalize()
            lev_val = leverage_obj.get("value", 1)
            leverage_str = f"{lev_type}{lev_val}x" if lev_type else f"{lev_val}x"
            try:
                leverage_num = float(lev_val)
            except (ValueError, TypeError):
                leverage_num = 1.0

            margin_used = float(pos.get("marginUsed", 0))
            if margin_used <= 0 and leverage_num > 0:
                margin_used = position_value / leverage_num

            positions.append({
                "coin": pos.get("coin"),
                "direction": "LONG" if szi > 0 else "SHORT",
                "entry_px": float(pos.get("entryPx", 0)),
                "mark_px": position_value / abs(szi),
                "val": position_value,
                "upnl": upnl,
                "pnl_pct": (upnl / margin_used * 100) if margin_used > 0 else 0.0,
                "leverage": leverage_str,
                "margin": margin_used,
            })
    except Exception as e:
        print(f"Błąd pobierania danych dla {address}: {e}")
        return None

    positions.sort(key=lambda x: x["val"], reverse=True)
    return account_equity, positions


# ---------------------------------------------------------------- Telegram

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    res = requests.post(url, json=payload, timeout=30)
    if not res.ok:
        print(f"Błąd Telegrama: {res.text}")
    res.raise_for_status()


# ---------------------------------------------------------------- dane

def collect_wallets():
    today_iso = datetime.now(TZ).strftime("%Y-%m-%d")
    history = load_history()
    wallets = []

    for cfg in load_wallet_configs("01_Wallet_Address.txt"):
        data = get_hyperliquid_data(cfg["address"])
        if data is None:
            continue
        equity, positions = data

        _, prev = get_previous_snapshot(history, cfg["address"], today_iso)

        # dzisiejszy snapshot pozycji (do oceny trendu następnego dnia)
        history.setdefault(cfg["address"], {})[today_iso] = {
            p["coin"]: {"direction": p["direction"], "val": p["val"], "mark_px": p["mark_px"]}
            for p in positions
        }

        if equity < MIN_EQUITY or not positions:
            continue
        wallets.append({
            "name": cfg["name"],
            "equity": equity,
            "positions": positions,
            "weight": wallet_weight(equity, positions),
            "prev": prev or {},
        })
    return wallets, today_iso, history


# ---------------------------------------------------------------- analiza

def build_pick(coin, direction, entries, agreement, dom, total_wallets):
    weights = [(e["w"]["weight"] * e["p"]["margin"], e) for e in entries]
    total_w = sum(w for w, _ in weights)
    if total_w <= 0:
        return None

    avg_entry = sum(e["p"]["entry_px"] * w for w, e in weights) / total_w
    if avg_entry <= 0:
        return None

    biggest = max(entries, key=lambda e: e["p"]["val"])
    price = biggest["p"]["mark_px"]
    avg_upnl = sum(e["p"]["pnl_pct"] * w for w, e in weights) / total_w

    lev_raw = weighted_median([(parse_leverage(e["p"]["leverage"]), w) for w, e in weights])
    leverage = int(max(1, min(MAX_LEVERAGE, round(lev_raw))))

    if direction == "LONG":
        limit = avg_entry * (1 + ENTRY_TOLERANCE)
        market_ok = price <= limit
    else:
        limit = avg_entry * (1 - ENTRY_TOLERANCE)
        market_ok = price >= limit

    entry = price if market_ok else limit
    sl_dist = MAX_LOSS_ON_MARGIN / leverage
    stop = entry * (1 - sl_dist) if direction == "LONG" else entry * (1 + sl_dist)

    size_pct = min(MAX_MARGIN_PCT, RISK_PER_TRADE / MAX_LOSS_ON_MARGIN * agreement) * 100
    size_pct = max(1, round(size_pct))

    return {
        "coin": coin,
        "direction": direction,
        "size_pct": size_pct,
        "icon": "🟢" if direction == "LONG" else "🔴",
        "wallets_n": len(entries),
        "wallets_total": total_wallets,
        "agreement": agreement,
        "avg_entry": avg_entry,
        "price": price,
        "avg_upnl": avg_upnl,
        "market_ok": market_ok,
        "entry": entry,
        "leverage": leverage,
        "whale_leverage": lev_raw,
        "stop": stop,
        "sl_dist": sl_dist,
        "up": sum(1 for e in entries if e["trend"] == "up"),
        "down": sum(1 for e in entries if e["trend"] == "down"),
        "score": dom * agreement,
    }


def analyze(wallets):
    by_coin = {}
    for w in wallets:
        for p in w["positions"]:
            tf, trend = trend_factor(w["prev"], p)
            share = min(1.0, p["margin"] / w["equity"]) if w["equity"] > 0 else 0.0
            conviction = w["weight"] * share * tf
            sides = by_coin.setdefault(p["coin"], {"LONG": [], "SHORT": []})
            sides[p["direction"]].append({"w": w, "p": p, "conv": conviction, "trend": trend})

    picks = []
    for coin, sides in by_coin.items():
        long_s = sum(e["conv"] for e in sides["LONG"])
        short_s = sum(e["conv"] for e in sides["SHORT"])
        total = long_s + short_s
        if total <= 0:
            continue

        direction = "LONG" if long_s >= short_s else "SHORT"
        dom = max(long_s, short_s)
        agreement = dom / total
        entries = sides[direction]

        if len(entries) < MIN_WALLETS or agreement < MIN_AGREEMENT:
            continue

        pick = build_pick(coin, direction, entries, agreement, dom, len(wallets))
        if pick:
            picks.append(pick)

    picks.sort(key=lambda x: x["score"], reverse=True)
    return picks[:MAX_PICKS]


# ---------------------------------------------------------------- komunikat

def build_message(picks, wallets, today_iso):
    total_equity = sum(w["equity"] for w in wallets)
    msg = (
        f"<b>📊 Analiza portfeli - propozycja pozycji</b> - {fmt_date(today_iso)}\n"
        f"Przeanalizowano: {len(wallets)} portfeli (Account Equity łącznie "
        f"{fmt_usd(total_equity, 2)})\n\n"
    )

    if not picks:
        msg += (
            "<i>Brak walut spełniających kryteria (min. "
            f"{MIN_WALLETS} portfele po tej samej stronie i zgodność "
            f"{MIN_AGREEMENT:.0%}).</i>\n\n"
        )

    for i, p in enumerate(picks, 1):
        if p["market_ok"]:
            entry_line = (
                f"Wejście: <b>{fmt_px(p['entry'])}</b> (cena rynkowa w strefie wejścia portfeli) "
                f"/ śr. wejście {fmt_px(p['avg_entry'])}"
            )
        else:
            gap = abs(p["price"] / p["avg_entry"] - 1) * 100
            side = "powyżej" if p["direction"] == "LONG" else "poniżej"
            entry_line = (
                f"Wejście: zlecenie limit <b>{fmt_px(p['entry'])}</b> "
                f"(cena {fmt_px(p['price'])} jest {gap:.1f}% {side} średniego wejścia portfeli) "
                f"/ śr. wejście {fmt_px(p['avg_entry'])}"
            )

        trend_parts = []
        if p["up"]:
            trend_parts.append(f"{p['up']} zwiększa")
        if p["down"]:
            trend_parts.append(f"{p['down']} redukuje")
        trend_line = f"Trend (zmiany): <b>{', '.join(trend_parts)}</b>\n" if trend_parts else ""

        if p["market_ok"]:
            size_txt = f"<b>{p['size_pct']}% kapitału</b>"
        else:
            size_txt = f"<b>0% teraz</b> (limit: {p['size_pct']}% kapitału)"

        msg += (
            f"<b>{i}. {p['coin']}</b> | {p['icon']} <b>{p['direction']}</b> - {size_txt}\n"
            f"Zgodność: {p['wallets_n']}/{p['wallets_total']} portfeli "
            f"({p['agreement']:.0%} wagi)\n"
            f"{entry_line}\n"
            f"Dźwignia: <b>{p['leverage']}x Isolated</b>\n"
            f"Stop-loss: <b>{fmt_px(p['stop'])}</b>\n"
            f"{trend_line}\n"
        )

    return msg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki i bez zapisu historii")
    args = parser.parse_args()

    wallets, today_iso, history = collect_wallets()

    if not args.dry_run:
        save_history(history)

    if not wallets:
        print("Brak portfeli do analizy")
        return

    picks = analyze(wallets)
    message = build_message(picks, wallets, today_iso)
    print(message)

    if not args.dry_run:
        send_telegram_message(message)


if __name__ == "__main__":
    main()
