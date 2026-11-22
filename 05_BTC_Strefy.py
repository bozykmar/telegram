"""
05_BTC_Strefy.py

Strefy skalowania pozycji BTC i ETH: symulacja dokładania do pozycji przy kolejnych progach
cenowych oraz wynikający z niej depozyt zabezpieczający i cena likwidacji. Wynik wysyłany jest
jako OSOBNY komunikat na Telegram (2x dziennie: 09:15 i 20:15, czas polski).

Konfiguracja (GitHub Actions variables, przekazane jako zmienne środowiskowe):
  TELEGRAM_TOKEN           - token bota
  TELEGRAM_CHAT_ID         - ID czatu / grupy
  TELEGRAM_THREAD_SIGNAL   - ID tematu w grupie (opcjonalnie)

Uruchomienie lokalne bez wysyłki:  python 05_BTC_Strefy.py --dry-run

Model (dla każdej pozycji, progi cenowe rosnąco, tylko progi nie wyższe niż aktualny kurs):
1. Przy przejściu przez próg depozyt rośnie o TOPUP_SHARE niezrealizowanego zysku od poprzedniego
   progu:  M += TOPUP_SHARE x q x (cena_progu - poprzednia_cena).
2. Wielkość dokupienia dq jest ograniczona dwoma warunkami (bierze się mniejszą wartość):
   a) dźwignia nie przekracza MAX_LEVERAGE:   dq <= MAX_LEVERAGE x M / cena - q,
   b) cena likwidacji pozostaje nie wyżej niż LIQ_RATIO x cena progu (po dokupieniu):
      dq <= (M - q x (avg - LIQ_RATIO x cena)) / ((1 - LIQ_RATIO) x cena).
3. Jeśli dq > 0, średnia cena wejścia avg i wielkość pozycji q są aktualizowane.
4. Cena likwidacji = avg - M / q.

Źródło kursu: Binance (data-api.binance.vision, potem api.binance.com), zapas: OKX.
Binance blokuje część adresów IP z USA, dlatego pierwszy jest publiczny mirror danych rynkowych.
"""

import sys
import argparse
from dataclasses import dataclass
from datetime import datetime

from common import NBSP, TZ, Telegram, fmt_usd, http_get

TG = Telegram("TELEGRAM_THREAD_SIGNAL")

BINANCE_URLS = (
    "https://data-api.binance.vision/api/v3/ticker/price",
    "https://api.binance.com/api/v3/ticker/price",
)
OKX_URL = "https://www.okx.com/api/v5/market/ticker"
USER_AGENT = "btc-strefy-bot"

# ---------------------------------------------------------------- parametry

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


# ---------------------------------------------------------------- kurs

def fetch_price(symbol):
    """Aktualny kurs: Binance (dwa adresy), w razie błędu OKX."""
    for url in BINANCE_URLS:
        try:
            return float(http_get(url, {"symbol": symbol}, user_agent=USER_AGENT)["price"])
        except Exception as e:  # noqa: BLE001
            print(f"[BLAD] {url} {symbol}: {e}")
    try:
        inst_id = symbol.replace("USDT", "-USDT")
        data = http_get(OKX_URL, {"instId": inst_id}, user_agent=USER_AGENT)
        return float(data["data"][0]["last"])
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Brak kursu {symbol}: {e}") from e


# ---------------------------------------------------------------- model

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


# ---------------------------------------------------------------- komunikat

def number(value):
    return f"{value:,.0f}".replace(",", NBSP)


def build_block(name, price, state):
    if state.threshold is None:
        threshold_line = "brak (kurs poniżej pierwszego progu)"
        margin_label = " (pozycja startowa)"
    else:
        threshold_line = f"<b>{number(state.threshold)}</b>"
        margin_label = ""
    return (
        f"<b>{name}</b> - kurs {fmt_usd(price, 2)}\n"
        f"Najniższa cena poniżej kursu: {threshold_line}\n"
        f"Depozyt zabezp.: <b>{number(state.margin)} USDT</b>{margin_label}\n"
        f"Likwidacja: <b>{fmt_usd(state.liquidation, 0)}</b>\n\n"
    )


def build_message(prices, today):
    msg = f"<b>📐 Strefy BTC / ETH</b> - {today}\n\n"
    for name, price in prices.items():
        state = simulate(POSITIONS[name], THRESHOLDS[name], price)
        msg += build_block(name, price, state)
    return msg


# ---------------------------------------------------------------- main

def collect_prices():
    prices = {}
    for name, position in POSITIONS.items():
        try:
            prices[name] = fetch_price(position.symbol)
        except RuntimeError as e:
            print(e)
    return prices


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki na Telegram")
    args = parser.parse_args()

    if not args.dry_run:
        TG.require()

    prices = collect_prices()
    if not prices:
        sys.exit("Brak kursów - przerywam.")

    message = build_message(prices, datetime.now(TZ).strftime("%d.%m.%Y"))
    print(message)

    if not args.dry_run:
        TG.send_message(message)


if __name__ == "__main__":
    main()
