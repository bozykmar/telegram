"""
01_Wallet_Whale.py

Raport pozycji portfeli z Hyperliquid wysyłany na Telegram (2x dziennie: 09:00 i 19:00).
Zestawia top 5 pozycji każdego portfela i porównuje je z poprzednim uruchomieniem.

Konfiguracja (GitHub Actions secrets / variables, przekazane jako zmienne środowiskowe):
  TELEGRAM_TOKEN         - token bota
  TELEGRAM_CHAT_ID       - ID czatu / grupy
  TELEGRAM_THREAD_WHALE  - ID tematu w grupie (opcjonalnie)

Lista portfeli: 01_Wallet_Address.txt (patrz opis formatu w pliku).
Historia: 01_Wallet_History.json (zatwierdzana do repozytorium przez workflow).

Lokalnie bez wysyłki i bez zapisu:  python 01_Wallet_Whale.py --dry-run
Wymuszenie slotu (np. przy opóźnionym cronie):  python 01_Wallet_Whale.py --slot 09
"""

import os
import re
import sys
import json
import time
import html
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def env(name):
    return (os.environ.get(name) or "").strip() or None


def env_int(name):
    value = env(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        print(f"Ignoruję {name}: '{value}' nie jest liczbą całkowitą.")
        return None


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = env_int("TELEGRAM_THREAD_WHALE")

API_URL = "https://api.hyperliquid.xyz/info"
HYPERBOT_URL = "https://hyperbot.network/trader/"
WALLET_FILE = "01_Wallet_Address.txt"
HISTORY_FILE = "01_Wallet_History.json"
HISTORY_KEEP = 60            # liczba przechowywanych zestawień (2 dziennie = 30 dni)
MIN_TRACK_USD = 100          # pozycje poniżej tej wartości nie trafiają do historii
MAX_CLOSED_SHOWN = 10        # ile zamkniętych pozycji pokazać w raporcie
TOP_N = 5                    # ile największych pozycji pokazać
TZ = ZoneInfo("Europe/Warsaw")
NBSP = "\u00a0"              # twarda spacja, żeby kwota nie łamała się w nowej linii

POSITION_CHANGE_THRESHOLD = 10.0  # % zmiany wielkości pozycji, od którego pojawia się 🟢 / 🔴
TELEGRAM_MAX_LEN = 4000           # limit Telegrama to 4096 znaków; zapas na bezpieczeństwo
TELEGRAM_ATTEMPTS = 3

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


SESSION = make_session()


# ---------------------------------------------------------------- formatowanie

def fmt_usd(value, decimals, signed=False):
    """
    >= 1000 USD: bez miejsc po przecinku, spacja jako separator tysięcy ($2 129).
    <  1000 USD: podana liczba miejsc po przecinku ($75.3510).
    signed=True: znak przed symbolem waluty (+$1 234 / -$56.20).
    """
    sign = "-" if value < 0 else ("+" if signed else "")
    abs_val = abs(value)
    if abs_val >= 1000:
        number = f"{abs_val:,.0f}".replace(",", NBSP)
    else:
        number = f"{abs_val:.{decimals}f}"
    return f"{sign}${number}"


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
    """Klucz zestawienia: data + slot (09 / 19). Slot można wymusić (--slot)."""
    if slot is None:
        slot = "09" if now.hour < 14 else "19"
    return f"{now:%Y-%m-%d}T{slot}"


def fmt_key(key):
    date_part, _, slot = key.partition("T")
    label = datetime.strptime(date_part, "%Y-%m-%d").strftime("%d.%m.%Y")
    return f"{label} {slot}:00" if slot else label


# ---------------------------------------------------------------- historia

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
    keep = set(sorted({k for w in history.values() for k in w})[-HISTORY_KEEP:])
    for address in list(history):
        history[address] = {k: s for k, s in history[address].items() if k in keep}
        if not history[address]:
            del history[address]
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY_FILE)


def get_previous_snapshot(history, address, key):
    """Zwraca (klucz, snapshot) z ostatniego zestawienia wcześniejszego niż bieżące."""
    days = history.get(address, {})
    older = sorted(k for k in days if k < key)
    if not older:
        return None, None
    return older[-1], days[older[-1]]


# ---------------------------------------------------------------- konfiguracja

def load_wallet_configs(file_path=WALLET_FILE):
    """
    Format linii (wszystko poza adresem opcjonalne):
        Nazwa - Opis - 0xAdres
        0xAdres
    Nadal działa też pełny link (https://hyperbot.network/trader/0x...).
    Linie zaczynające się od '#' oraz puste są pomijane.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Nie znaleziono pliku {file_path}")

    wallets, seen = [], set()
    with open(file_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = ADDRESS_RE.search(line)
            if not match:
                print(f"Pominięto linię bez poprawnego adresu 0x…: {line[:60]}")
                continue

            address = match.group(0).lower()
            if address in seen:
                continue
            seen.add(address)

            head = re.sub(r"https?://\S*$", "", line[:match.start()])
            head = head.rstrip(" \t-–—|:")
            parts = [p.strip() for p in head.split(" - ") if p.strip()]
            name = parts[0] if parts else f"Wallet {address[:6]}…{address[-4:]}"
            wallets.append({
                "name": name,
                "description": " - ".join(parts[1:]),
                "link": HYPERBOT_URL + address,
                "address": address,
            })
    return wallets


# ---------------------------------------------------------------- Hyperliquid

def get_hyperliquid_data(address):
    """
    Zwraca (account_equity, lista_pozycji) lub None przy błędzie
    (wtedy snapshot nie jest zapisywany).
    Udział pozycji = margin pozycji / suma marginów wszystkich otwartych pozycji.
    """
    positions = []
    try:
        res = SESSION.post(API_URL, json={"type": "clearinghouseState", "user": address}, timeout=30)
        res.raise_for_status()
        data = res.json()

        account_equity = float(data.get("marginSummary", {}).get("accountValue", 0))

        for item in data.get("assetPositions", []):
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
                lev_num = float(lev_val)
            except (ValueError, TypeError):
                lev_num = 1.0

            margin_used = float(pos.get("marginUsed", 0))
            if margin_used <= 0 and lev_num > 0:
                margin_used = position_value / lev_num

            positions.append({
                "coin": pos.get("coin"),
                "direction": "LONG" if szi > 0 else "SHORT",
                "color_icon": "🟢" if szi > 0 else "🔴",
                "entry_px": float(pos.get("entryPx", 0)),
                "mark_px": position_value / abs(szi),
                "val": position_value,
                "upnl": upnl,
                "pnl_pct": (upnl / margin_used * 100) if margin_used > 0 else 0.0,
                "leverage": leverage_str,
                "margin": margin_used,
            })

        total_margin = sum(p["margin"] for p in positions)
        for p in positions:
            p["portfolio_pct"] = (p["margin"] / total_margin * 100) if total_margin > 0 else 0.0

    except Exception as e:  # noqa: BLE001
        print(f"Błąd pobierania danych dla {address}: {e}")
        return None

    positions.sort(key=lambda x: x["val"], reverse=True)
    return account_equity, positions


# ---------------------------------------------------------------- Telegram

def split_message(message, limit=TELEGRAM_MAX_LEN):
    """Dzieli tekst na części po liniach (tagi HTML w liniach są zamknięte)."""
    if len(message) <= limit:
        return [message]
    chunks, current = [], ""
    for line in message.splitlines(keepends=True):
        while len(line) > limit:  # awaryjnie: pojedyncza zbyt długa linia
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


def send_telegram_chunk(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if TELEGRAM_THREAD_ID is not None:
        payload["message_thread_id"] = TELEGRAM_THREAD_ID

    last_error = None
    for attempt in range(TELEGRAM_ATTEMPTS):
        try:
            res = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            last_error = e
            time.sleep(2 * (attempt + 1))
            continue

        if res.status_code == 429:  # limit Telegrama - odczekaj zalecany czas
            try:
                wait = int(res.json().get("parameters", {}).get("retry_after", 5))
            except Exception:  # noqa: BLE001
                wait = 5
            last_error = requests.HTTPError(f"429 Too Many Requests: {res.text}")
            time.sleep(wait + 1)
            continue

        if not res.ok:
            print(f"Błąd Telegrama: {res.text}")
        res.raise_for_status()
        return

    raise last_error or RuntimeError("Nie udało się wysłać wiadomości na Telegram")


def send_telegram_message(message):
    chunks = split_message(message)
    for i, chunk in enumerate(chunks):
        send_telegram_chunk(chunk)
        if i < len(chunks) - 1:
            time.sleep(1.1)


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
        send_telegram_message(message)

    if ok and not dry_run:
        history.setdefault(config["address"], {})[key] = snapshot_of(*data)
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki i bez zapisu historii")
    parser.add_argument("--slot", choices=["09", "19"], help="wymuś slot zestawienia (domyślnie wg godziny)")
    args = parser.parse_args()

    if not args.dry_run and not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        sys.exit("Brak TELEGRAM_TOKEN lub TELEGRAM_CHAT_ID w zmiennych środowiskowych.")

    wallets = load_wallet_configs()
    if not wallets:
        sys.exit(f"Brak poprawnych adresów w pliku {WALLET_FILE}")

    key = run_key(datetime.now(TZ), args.slot)
    history = load_history()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda w: get_hyperliquid_data(w["address"]), wallets))

    errors = 0
    for config, data in zip(wallets, results):
        try:
            if not process_wallet(config, data, history, key, args.dry_run):
                errors += 1
        except Exception as e:  # noqa: BLE001
            print(f"Błąd przetwarzania {config['name']}: {e}")
            errors += 1
        if not args.dry_run:
            time.sleep(1.1)  # limit Telegrama: ok. 20 wiadomości/min na grupę

    if not args.dry_run:
        save_history(history)

    if errors:
        sys.exit(f"Błędy: {errors}/{len(wallets)} portfeli")


if __name__ == "__main__":
    main()
