"""
01_Wallet_Whale.py

Raport pozycji portfeli z Hyperliquid wysyłany na Telegram (2x dziennie: 09:15 i 20:15, czas polski).
Zestawia top 5 pozycji każdego portfela i porównuje je z poprzednim uruchomieniem.

Konfiguracja (GitHub Actions secrets / variables, przekazane jako zmienne środowiskowe):
  TELEGRAM_TOKEN         - token bota
  TELEGRAM_CHAT_ID       - ID czatu / grupy
  TELEGRAM_THREAD_WHALE  - ID tematu w grupie (opcjonalnie)

Lista portfeli: 01_Wallet_Address.txt (patrz opis formatu w common.load_wallet_configs).
Historia: 01_Wallet_History.json (zatwierdzana do repozytorium przez workflow).

Lokalnie bez wysyłki i bez zapisu:  python 01_Wallet_Whale.py --dry-run
Wymuszenie slotu (np. przy opóźnionym cronie):  python 01_Wallet_Whale.py --slot 09

Kod wyjścia: 0 = OK lub błędy częściowe (ostrzeżenie w logu), 1 = zawiodły wszystkie portfele.
"""

import sys
import time
import html
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from common import (TZ, Telegram, fmt_usd, get_hyperliquid_data, load_json, load_wallet_configs,
                    save_json)

TG = Telegram("TELEGRAM_THREAD_WHALE")

WALLET_FILE = "01_Wallet_Address.txt"
HISTORY_FILE = "01_Wallet_History.json"
HISTORY_KEEP = 60            # liczba przechowywanych zestawień (2 dziennie = 30 dni)
MIN_TRACK_USD = 100          # pozycje poniżej tej wartości nie trafiają do historii
MAX_CLOSED_SHOWN = 10        # ile zamkniętych pozycji pokazać w raporcie
TOP_N = 5                    # ile największych pozycji pokazać
POSITION_CHANGE_THRESHOLD = 10.0  # % zmiany wielkości pozycji, od którego pojawia się 🟢 / 🔴


# ---------------------------------------------------------------- formatowanie

def fmt_pct_portfolio(value):
    return f"{value:.0f}%" if value >= 1 else f"{value:.1f}%"


def change_pct_value(old, new):
    """Zmiana procentowa jako liczba (None, gdy brak punktu odniesienia)."""
    if not old:
        return None
    return (new - old) / abs(old) * 100


def fmt_pct_signed(change, auto=False):
    """Formatuje gotową zmianę procentową. auto=True: bez miejsc po przecinku przy >= 100%."""
    if change is None:
        return "n/d"
    if auto and abs(change) >= 100:
        return f"{change:+.0f}%"
    return f"{change:+.1f}%"


def fmt_change_pct(old, new, auto=False):
    return fmt_pct_signed(change_pct_value(old, new), auto=auto)


def change_marker(change):
    """Telegram nie obsługuje kolorów tekstu - znacznik 🟢 / 🔴 przy zmianie powyżej progu."""
    if change is None:
        return ""
    if change > POSITION_CHANGE_THRESHOLD:
        return "🟢 "
    if change < -POSITION_CHANGE_THRESHOLD:
        return "🔴 "
    return ""


def run_key(now, slot=None):
    """Klucz zestawienia: data + slot (09 = 09:15, 20 = 20:15). Slot można wymusić (--slot)."""
    if slot is None:
        slot = "09" if now.hour < 14 else "20"
    return f"{now:%Y-%m-%d}T{slot}"


def fmt_key(key):
    date_part, _, slot = key.partition("T")
    label = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d.%m.%Y")
    if not slot:
        return label
    minutes = "15" if slot in ("09", "20") else "00"  # starsze wpisy (np. 19) mają pełną godzinę
    return f"{label} {slot}:{minutes}"


# ---------------------------------------------------------------- historia

def save_history(history):
    keep = set(sorted({k for w in history.values() for k in w})[-HISTORY_KEEP:])
    for address in list(history):
        history[address] = {k: s for k, s in history[address].items() if k in keep}
        if not history[address]:
            del history[address]
    save_json(HISTORY_FILE, history, indent=1)


def get_previous_snapshot(history, address, key):
    """Zwraca (klucz, snapshot) z ostatniego zestawienia wcześniejszego niż bieżące."""
    days = history.get(address, {})
    older = sorted(k for k in days if k < key)
    if not older:
        return None, None
    return older[-1], days[older[-1]]


# ---------------------------------------------------------------- komunikat

def size_of(pos):
    """Wielkość pozycji w jednostkach monety (None, gdy brak ceny)."""
    if not pos.get("mark_px"):
        return None
    return pos["val"] / pos["mark_px"]


def size_change_pct(prev_pos, p):
    """Zmiana wielkości pozycji w jednostkach monety (bez wpływu ruchu ceny)."""
    old, new = size_of(prev_pos), size_of(p)
    if old is None or new is None:
        return None
    return change_pct_value(old, new)


def build_position_block(p, prev_pos, prev_label, has_history):
    msg = (
        f"<b>{html.escape(str(p['coin']))}</b> | {p['color_icon']} <b>{p['direction']}</b> "
        f"<b>{p['leverage']}</b> - {fmt_pct_portfolio(p['portfolio_pct'])} portfela\n"
        f"Price <b>{fmt_usd(p['mark_px'], 4)}</b> / Avg. Price: <b>{fmt_usd(p['entry_px'], 4)}</b>\n"
        f"Position: <b>{fmt_usd(p['val'], 2)}</b> / uPnL: <b>{fmt_usd(p['upnl'], 2, True)}</b> "
        f"(<b>{p['pnl_pct']:+.2f}%</b>)\n"
    )
    if not has_history:
        return msg + "\n"

    if prev_pos is None:
        msg += "<i>Nowa pozycja (brak w poprzednim zestawieniu)</i>\n"
    else:
        size_chg = size_change_pct(prev_pos, p)
        msg += (
            f"<i>{prev_label}:</i> Price {fmt_usd(prev_pos['mark_px'], 4)} / "
            f"Position: {fmt_usd(prev_pos['val'], 2)} / "
            f"uPnL: {prev_pos['pnl_pct']:+.2f}%\n"
            f"<i>Zmiana:</i> Price {fmt_change_pct(prev_pos['mark_px'], p['mark_px'])} / "
            f"Position {fmt_change_pct(prev_pos['val'], p['val'])} / "
            f"{change_marker(size_chg)}<b>Size {fmt_pct_signed(size_chg)}</b> / "
            f"uPnL {fmt_change_pct(prev_pos['upnl'], p['upnl'], auto=True)}\n"
        )
    return msg + "\n"


def build_wallet_message(config, data, history, key):
    name = html.escape(config["name"])
    parts = [f'<a href="{config["link"]}">{name}</a>']
    if config["description"]:
        parts.append(f'<i>{html.escape(config["description"])}</i>')
    parts.append(fmt_key(key))
    msg = " - ".join(parts) + "\n"

    account_equity, all_positions = data
    prev_key, prev_snap = get_previous_snapshot(history, config["address"], key)
    prev_label = fmt_key(prev_key) if prev_key else None
    prev_positions = prev_snap["positions"] if prev_snap else {}

    if prev_snap:
        msg += (f"Account Equity: <b>{fmt_usd(account_equity, 2)}</b> "
                f"(zmiana {fmt_change_pct(prev_snap['account_value'], account_equity)})\n\n")
    else:
        msg += f"Account Equity: <b>{fmt_usd(account_equity, 2)}</b>\n\n"

    if not all_positions:
        msg += "<i>Brak aktywnych pozycji na portfelu.</i>\n\n"

    for p in all_positions[:TOP_N]:
        prev_pos = prev_positions.get(p["coin"])
        if prev_pos and prev_pos["direction"] != p["direction"]:
            prev_pos = None  # zmiana kierunku = nowa pozycja
        msg += build_position_block(p, prev_pos, prev_label, bool(prev_snap))

    if prev_snap:
        current = {p["coin"]: p for p in all_positions}
        closed = sorted(
            ((c, pp) for c, pp in prev_positions.items()
             if c not in current or current[c]["direction"] != pp["direction"]),
            key=lambda x: x[1]["val"], reverse=True,
        )
        if closed:
            shown = ", ".join(
                f"{html.escape(str(c))} {pp['direction']} ({fmt_usd(pp['val'], 2)})"
                for c, pp in closed[:MAX_CLOSED_SHOWN]
            )
            more = len(closed) - MAX_CLOSED_SHOWN
            if more > 0:
                shown += f" i {more} mniejszych"
            msg += f"<b>Zamknięte od {prev_label}:</b> {shown}\n"
    else:
        msg += "<i>Brak danych historycznych. Porównanie będzie dostępne od kolejnego uruchomienia.</i>\n"

    return msg.rstrip() + "\n"


def snapshot_of(account_equity, positions):
    return {
        "account_value": round(account_equity, 2),
        "positions": {
            p["coin"]: {
                "direction": p["direction"],
                "val": round(p["val"], 2),
                "upnl": round(p["upnl"], 2),
                "pnl_pct": round(p["pnl_pct"], 2),
                "entry_px": p["entry_px"],
                "mark_px": p["mark_px"],
            }
            for p in positions if p["val"] >= MIN_TRACK_USD
        },
    }


def process_wallet(config, data, history, key, dry_run):
    """Zwraca True, gdy portfel przetworzony i wysłany bez błędu."""
    if data is None:
        header = f'<a href="{config["link"]}">{html.escape(config["name"])}</a> - {fmt_key(key)}\n'
        message, ok = header + "\n<i>Nie udało się pobrać danych portfela.</i>\n", False
    else:
        message, ok = build_wallet_message(config, data, history, key), True

    if dry_run:
        print(message)
    else:
        TG.send_message(message)

    if ok and not dry_run:
        history.setdefault(config["address"], {})[key] = snapshot_of(*data)
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki i bez zapisu historii")
    parser.add_argument("--slot", choices=["09", "20"], help="wymuś slot zestawienia (domyślnie wg godziny)")
    args = parser.parse_args()

    if not args.dry_run:
        TG.require()

    wallets = load_wallet_configs(WALLET_FILE)
    if not wallets:
        sys.exit(f"Brak poprawnych adresów w pliku {WALLET_FILE}")

    key = run_key(datetime.now(TZ), args.slot)
    history = load_json(HISTORY_FILE)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda w: get_hyperliquid_data(w["address"]), wallets))

    errors = 0
    try:
        for config, data in zip(wallets, results):
            try:
                if not process_wallet(config, data, history, key, args.dry_run):
                    errors += 1
            except Exception as e:  # noqa: BLE001
                print(f"Błąd przetwarzania {config['name']}: {e}")
                errors += 1
            if not args.dry_run:
                time.sleep(1.1)  # limit Telegrama: ok. 20 wiadomości/min na grupę
    finally:
        if not args.dry_run:
            try:
                save_history(history)
            except OSError as e:
                print(f"Nie udało się zapisać historii: {e}")

    if errors:
        print(f"Ostrzeżenie: błędy w {errors}/{len(wallets)} portfelach.")
    if errors == len(wallets):
        sys.exit(1)  # awaria całkowita - workflow powinien to zgłosić


if __name__ == "__main__":
    main()
