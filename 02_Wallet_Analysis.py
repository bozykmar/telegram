"""
02_Wallet_Analysis.py

Informacyjna analiza pozycji portfeli z 01_Wallet_Address.txt, wysyłana jako OSOBNY komunikat
na Telegram (2x dziennie: 09:15 i 20:15, czas polski).

Skrypt NIE zaleca pozycji i nie odnosi się do kapitału użytkownika: pokazuje wyłącznie to,
co robią obserwowane portfele - kierunek, wielkość portfeli i ich pozycji, dźwignię, średnie
wejście i zmiany od poprzedniego dnia. Nie ma tu stop-lossów, zleceń limit ani procentów kapitału.

Skrypt nie importuje 01_Wallet_Whale.py - korzysta tylko ze wspólnego common.py i pliku
01_Wallet_Address.txt. Własną historię pozycji (do oceny trendu) zapisuje w
02_Analysis_History.json.

Konfiguracja (GitHub Actions variables, przekazane jako zmienne środowiskowe):
  TELEGRAM_TOKEN            - token bota
  TELEGRAM_CHAT_ID          - ID czatu / grupy
  TELEGRAM_THREAD_ANALYSIS  - ID tematu w grupie (opcjonalnie)

Uruchomienie lokalne bez wysyłki i bez zapisu:  python 02_Wallet_Analysis.py --dry-run

Metodyka (heurystyka konsensusu, nie model predykcyjny):
1. Waga portfela = log10(Account Equity) x (1 + ROE otwartych pozycji, ucięte do +-50%).
2. Przekonanie pozycji = waga portfela x udział marginu pozycji w Account Equity
   x współczynnik trendu (zwiększanie pozycji od poprzedniego dnia +15%, redukcja -15%).
3. Dla każdej waluty sumujemy przekonanie po stronie LONG i SHORT. Kierunek portfeli to strona
   z większym przekonaniem, zgodność = jej udział w sumie obu stron.
4. Wybór: MAX_COINS walut z największym zaangażowanym kapitałem (suma marginów portfeli po obu
   stronach), od największego. Portfele z Account Equity poniżej MIN_EQUITY są pomijane.
5. Średnie wejście = średnia cen wejścia portfeli po dominującej stronie, ważona wagą portfela
   i marginem pozycji. Dźwignia = mediana ważona dźwigni tych portfeli.
6. Zmiana pozycji = zmiana wielkości pozycji (w jednostkach monety) od poprzedniego dnia;
   przy redukcji komunikat podaje, o ile procent pozycja się zmniejszyła.
"""

import re
import html
import math
import argparse
from datetime import datetime

from common import (TZ, Telegram, fmt_usd, get_hyperliquid_data, load_json,
                    load_wallet_configs, save_json)

TG = Telegram("TELEGRAM_THREAD_ANALYSIS")

WALLET_FILE = "01_Wallet_Address.txt"
HISTORY_FILE = "02_Analysis_History.json"
HISTORY_DAYS = 30

# ---------------------------------------------------------------- parametry

MAX_COINS = 3             # ile walut pokazać (największy zaangażowany kapitał)
MAX_WALLET_LINES = 3      # ile największych portfeli wypisać przy każdej walucie
MIN_EQUITY = 50_000       # portfele poniżej tej wartości są pomijane (USD)


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


def lev_label(text):
    """'Isolated5x' -> 'Isolated 5x'."""
    m = re.match(r"\s*([A-Za-z]*)\s*(\d+(?:\.\d+)?)x", text or "")
    if not m:
        return "1x"
    value = f"{float(m.group(2)):g}x"
    return f"{m.group(1)} {value}" if m.group(1) else value


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
    """
    Porównanie wielkości pozycji (w jednostkach monety) z poprzednim dniem.
    Zwraca (współczynnik, etykieta, zmiana względna), np. (0.85, "down", -0.23).
    """
    prev = prev_positions.get(p["coin"])
    if (
        not prev
        or prev.get("direction") != p["direction"]
        or not prev.get("mark_px")
        or not p["mark_px"]
    ):
        return 1.0, "new", 0.0
    size_prev = prev["val"] / prev["mark_px"]
    size_now = p["val"] / p["mark_px"]
    if size_prev <= 0:
        return 1.0, "new", 0.0
    change = size_now / size_prev - 1
    if change > 0.05:
        return 1.15, "up", change
    if change < -0.05:
        return 0.85, "down", change
    return 1.0, "flat", change


def fmt_date(iso_date):
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")


# ---------------------------------------------------------------- historia (własna)

def save_history(history):
    cutoff = sorted({d for w in history.values() for d in w})[-HISTORY_DAYS:]
    for address in history:
        history[address] = {d: s for d, s in history[address].items() if d in cutoff}
    save_json(HISTORY_FILE, history, indent=2)


def get_previous_snapshot(history, address, today_iso):
    """Zwraca (data, pozycje) z ostatniego dnia wcześniejszego niż dzisiaj."""
    days = history.get(address, {})
    older = sorted(d for d in days if d < today_iso)
    if not older:
        return None, None
    return older[-1], days[older[-1]]


# ---------------------------------------------------------------- dane

def collect_wallets():
    today_iso = datetime.now(TZ).strftime("%Y-%m-%d")
    history = load_json(HISTORY_FILE)
    wallets = []

    for cfg in load_wallet_configs(WALLET_FILE):
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

def summarize_coin(coin, sides, total_wallets):
    """Podsumowanie jednej waluty: kierunek portfeli, zaangażowanie, średnie wejście, lista portfeli."""
    long_s = sum(e["conv"] for e in sides["LONG"])
    short_s = sum(e["conv"] for e in sides["SHORT"])
    total = long_s + short_s
    if total <= 0:
        return None

    direction = "LONG" if long_s >= short_s else "SHORT"
    entries = sides[direction]
    agreement = max(long_s, short_s) / total

    weights = [(e["w"]["weight"] * e["p"]["margin"], e) for e in entries]
    total_w = sum(w for w, _ in weights)
    if total_w <= 0:
        return None
    avg_entry = sum(e["p"]["entry_px"] * w for w, e in weights) / total_w
    lev_med = weighted_median([(parse_leverage(e["p"]["leverage"]), w) for w, e in weights])

    everyone = sides["LONG"] + sides["SHORT"]
    biggest = max(everyone, key=lambda e: e["p"]["val"])

    return {
        "coin": coin,
        "direction": direction,
        "icon": "🟢" if direction == "LONG" else "🔴",
        "agreement": agreement,
        "wallets_total": total_wallets,
        "n_long": len(sides["LONG"]),
        "n_short": len(sides["SHORT"]),
        "margin_long": sum(e["p"]["margin"] for e in sides["LONG"]),
        "margin_short": sum(e["p"]["margin"] for e in sides["SHORT"]),
        "notional": sum(e["p"]["val"] for e in everyone),
        "avg_entry": avg_entry if avg_entry > 0 else None,
        "price": biggest["p"]["mark_px"],
        "leverage_median": lev_med,
        "lev_min": min(parse_leverage(e["p"]["leverage"]) for e in entries),
        "lev_max": max(parse_leverage(e["p"]["leverage"]) for e in entries),
        "up": sum(1 for e in entries if e["trend"] == "up"),
        "down": sum(1 for e in entries if e["trend"] == "down"),
        "entries": sorted(entries, key=lambda e: e["p"]["margin"], reverse=True),
    }


def analyze(wallets):
    by_coin = {}
    for w in wallets:
        for p in w["positions"]:
            tf, trend, change = trend_factor(w["prev"], p)
            share = min(1.0, p["margin"] / w["equity"]) if w["equity"] > 0 else 0.0
            conviction = w["weight"] * share * tf
            sides = by_coin.setdefault(p["coin"], {"LONG": [], "SHORT": []})
            sides[p["direction"]].append(
                {"w": w, "p": p, "conv": conviction, "trend": trend, "change": change})

    coins = [c for c in (summarize_coin(coin, sides, len(wallets))
                         for coin, sides in by_coin.items()) if c]
    coins.sort(key=lambda c: c["margin_long"] + c["margin_short"], reverse=True)
    return coins[:MAX_COINS]


# ---------------------------------------------------------------- komunikat

def trend_text(e):
    if e["trend"] == "up":
        return " - zwiększa"
    if e["trend"] == "down":
        return f" - redukuje o {abs(e['change']) * 100:.0f}%"
    return ""


def build_wallet_line(e):
    w, p = e["w"], e["p"]
    share = p["margin"] / w["equity"] if w["equity"] > 0 else 0.0
    return (
        f"• {html.escape(w['name'])}: equity {fmt_usd(w['equity'], 0)}, "
        f"margin {fmt_usd(p['margin'], 0)} ({share:.0%} equity), {lev_label(p['leverage'])}, "
        f"wejście {fmt_px(p['entry_px'])}, PnL {p['pnl_pct']:+.0f}%"
        f"{trend_text(e)}"
    )


def margin_split(c):
    """Udział marginu LONG i SHORT w % (suma zawsze 100)."""
    total = c["margin_long"] + c["margin_short"]
    if total <= 0:
        return 0, 0
    long_pct = round(c["margin_long"] / total * 100)
    return long_pct, 100 - long_pct


def build_coin_block(i, c):
    long_pct, short_pct = margin_split(c)
    lines = [
        f"<b>{i}. {c['coin']}</b> | {c['icon']} <b>{c['direction']}</b> "
        f"(zgodność {c['agreement']:.0%} wagi)",
        f"Zaangażowanie portfeli: <b>{fmt_usd(c['margin_long'] + c['margin_short'], 0)}</b> marginu "
        f"(nominalnie {fmt_usd(c['notional'], 0)})",
        f"Portfele: <b>{c['n_long']} LONG / {c['n_short']} SHORT</b> "
        f"(z {c['wallets_total']} analizowanych), margin "
        f"<b>LONG {fmt_usd(c['margin_long'], 0)} ({long_pct}%) / "
        f"SHORT {fmt_usd(c['margin_short'], 0)} ({short_pct}%)</b>",
    ]
    if c["avg_entry"]:
        gap = (c["price"] / c["avg_entry"] - 1) * 100
        lines.append(f"Śr. wejście portfeli: <b>{fmt_px(c['avg_entry'])}</b> | "
                     f"cena <b>{fmt_px(c['price'])}</b> ({gap:+.1f}% od śr. wejścia)")
    lines.append(f"Dźwignia portfeli: mediana <b>{c['leverage_median']:g}x</b> "
                 f"(zakres {c['lev_min']:g}x-{c['lev_max']:g}x)")

    trend_parts = []
    if c["up"]:
        trend_parts.append(f"{c['up']} zwiększa")
    if c["down"]:
        trend_parts.append(f"{c['down']} redukuje")
    if trend_parts:
        lines.append(f"Zmiany od poprzedniego dnia: <b>{', '.join(trend_parts)}</b>")
    lines.append("")      # zawsze jedna pusta linia przed listą portfeli

    shown = c["entries"][:MAX_WALLET_LINES]
    lines += [build_wallet_line(e) for e in shown]
    if len(c["entries"]) > len(shown):
        lines.append(f"… i {len(c['entries']) - len(shown)} kolejnych portfeli po tej stronie")
    return "\n".join(lines) + "\n\n"


def build_message(coins, wallets, today_iso):
    total_equity = sum(w["equity"] for w in wallets)
    msg = (
        f"<b>📊 Analiza portfeli - pozycje</b> - {fmt_date(today_iso)}\n"
        f"Przeanalizowano: {len(wallets)} portfeli (Account Equity łącznie "
        f"{fmt_usd(total_equity, 2)})\n\n"
    )
    if not coins:
        msg += "<i>Brak otwartych pozycji w analizowanych portfelach.</i>\n\n"
    for i, c in enumerate(coins, 1):
        msg += build_coin_block(i, c)
    return msg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki i bez zapisu historii")
    args = parser.parse_args()

    if not args.dry_run:
        TG.require()

    wallets, today_iso, history = collect_wallets()

    if not args.dry_run:
        save_history(history)

    if not wallets:
        print("Brak portfeli do analizy")
        return

    message = build_message(analyze(wallets), wallets, today_iso)
    print(message)

    if not args.dry_run:
        TG.send_message(message)


if __name__ == "__main__":
    main()
