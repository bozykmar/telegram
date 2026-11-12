import os
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

API_URL = "https://api.hyperliquid.xyz/info"
HEADERS = {"Content-Type": "application/json"}

HISTORY_FILE = "01_Wallet_History.json"
HISTORY_DAYS = 30  # ile dni historii przechowywać
TZ = ZoneInfo("Europe/Warsaw")

NBSP = "\u00a0"  # twarda spacja, żeby kwota nie łamała się w nowej linii

POSITION_CHANGE_THRESHOLD = 10.0  # % zmiany pozycji, od którego pojawia się 🟢 / 🔴


# ---------------------------------------------------------------- formatowanie

def fmt_usd(value, decimals, signed=False):
    """
    >= 1000 USD: bez miejsc po przecinku, spacja jako separator tysięcy ($2 129).
    <  1000 USD: podana liczba miejsc po przecinku ($75.3510).
    """
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


def fmt_pct_portfolio(value):
    return f"{value:.0f}%" if value >= 1 else f"{value:.1f}%"


def change_pct_value(old, new):
    """Zmiana procentowa jako liczba (None, gdy brak punktu odniesienia)."""
    if old == 0:
        return None
    return (new - old) / abs(old) * 100


def fmt_change_pct(old, new, auto=False):
    """auto=True: bez miejsc po przecinku przy zmianach >= 100% (np. uPnL)."""
    change = change_pct_value(old, new)
    if change is None:
        return "n/d"
    if auto and abs(change) >= 100:
        return f"{change:+.0f}%"
    return f"{change:+.1f}%"


def change_marker(change):
    """
    Telegram nie obsługuje kolorów tekstu, więc kolor zastępuje znacznik:
    🟢 przy wzroście o więcej niż 10%, 🔴 przy spadku o więcej niż 10%.
    """
    if change is None:
        return ""
    if change > POSITION_CHANGE_THRESHOLD:
        return "🟢 "
    if change < -POSITION_CHANGE_THRESHOLD:
        return "🔴 "
    return ""


def fmt_date(iso_date):
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")


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
    cutoff = sorted({d for w in history.values() for d in w})[-HISTORY_DAYS:]
    for address in history:
        history[address] = {d: s for d, s in history[address].items() if d in cutoff}
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_previous_snapshot(history, address, today_iso):
    """Zwraca (data, snapshot) z ostatniego dnia wcześniejszego niż dzisiaj."""
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

        wallet_address = link.split("/trader/")[-1].strip().split("?")[0].strip("/")
        name = parts[0]
        description = " - ".join(parts[1:-1])

        if wallet_address:
            wallets.append({
                "name": name,
                "description": description,
                "link": link,
                "address": wallet_address,
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
        payload = {"type": "clearinghouseState", "user": address}
        res = requests.post(API_URL, json=payload, headers=HEADERS, timeout=30)
        res.raise_for_status()
        res_perp = res.json()

        account_equity = float(res_perp.get("marginSummary", {}).get("accountValue", 0))
        asset_positions = res_perp.get("assetPositions", [])

        for item in asset_positions:
            pos = item.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi == 0:
                continue

            coin = pos.get("coin")
            entry_px = float(pos.get("entryPx", 0))
            position_value = float(pos.get("positionValue", 0))
            upnl = float(pos.get("unrealizedPnl", 0))
            direction = "LONG" if szi > 0 else "SHORT"
            color_icon = "🟢" if szi > 0 else "🔴"

            mark_px = position_value / abs(szi)

            leverage_obj = pos.get("leverage", {})
            lev_type = leverage_obj.get("type", "").capitalize()
            lev_val = leverage_obj.get("value", 1)
            leverage_str = f"{lev_type}{lev_val}x" if lev_type else f"{lev_val}x"

            try:
                leverage_val_num = float(lev_val)
            except (ValueError, TypeError):
                leverage_val_num = 1.0

            margin_used = float(pos.get("marginUsed", 0))
            if margin_used <= 0 and leverage_val_num > 0:
                margin_used = position_value / leverage_val_num

            pnl_pct = (upnl / margin_used * 100) if margin_used > 0 else 0.0

            positions.append({
                "coin": coin,
                "direction": direction,
                "color_icon": color_icon,
                "entry_px": entry_px,
                "mark_px": mark_px,
                "val": position_value,
                "upnl": upnl,
                "pnl_pct": pnl_pct,
                "leverage": leverage_str,
                "margin": margin_used,
            })

        # Udział w całkowitym marginie portfela (suma wszystkich pozycji = 100%)
        total_margin = sum(p["margin"] for p in positions)
        for p in positions:
            p["portfolio_pct"] = (p["margin"] / total_margin * 100) if total_margin > 0 else 0.0

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
    res = requests.post(url, json=payload, timeout=30)
    if not res.ok:
        print(f"Błąd Telegrama: {res.text}")
    res.raise_for_status()


def build_position_block_no_history(p):
    return (
        f"<b>{p['coin']}</b> | {p['color_icon']} <b>{p['direction']}</b> "
        f"<b>{p['leverage']}</b> - {fmt_pct_portfolio(p['portfolio_pct'])} portfela\n"
        f"Price <b>{fmt_usd(p['mark_px'], 4)}</b> / Avg. Price: <b>{fmt_usd(p['entry_px'], 4)}</b>\n"
        f"Position: <b>{fmt_usd(p['val'], 2)}</b> / uPnL: <b>{fmt_usd(p['upnl'], 2, True)}</b> "
        f"(<b>{p['pnl_pct']:+.2f}%</b>)\n\n"
    )


def build_position_block(p, prev_pos, prev_label):
    msg = (
        f"<b>{p['coin']}</b> | {p['color_icon']} <b>{p['direction']}</b> "
        f"<b>{p['leverage']}</b> - {fmt_pct_portfolio(p['portfolio_pct'])} portfela\n"
        f"Price <b>{fmt_usd(p['mark_px'], 4)}</b> / Avg. Price: <b>{fmt_usd(p['entry_px'], 4)}</b>\n"
        f"Position: <b>{fmt_usd(p['val'], 2)}</b> / uPnL: <b>{fmt_usd(p['upnl'], 2, True)}</b> "
        f"(<b>{p['pnl_pct']:+.2f}%</b>)\n"
    )

    if prev_pos is None:
        msg += "<i>Nowa pozycja (brak w poprzednim zestawieniu)</i>\n"
    else:
        pos_change = change_pct_value(prev_pos["val"], p["val"])
        msg += (
            f"<i>{prev_label}:</i> Price {fmt_usd(prev_pos['mark_px'], 4)} / "
            f"Position: {fmt_usd(prev_pos['val'], 2)} / "
            f"uPnL: {prev_pos['pnl_pct']:+.2f}%\n"
            f"<i>Zmiana:</i> Price {fmt_change_pct(prev_pos['mark_px'], p['mark_px'])} / "
            f"{change_marker(pos_change)}<b>Position {fmt_change_pct(prev_pos['val'], p['val'])}</b> / "
            f"uPnL {fmt_change_pct(prev_pos['upnl'], p['upnl'], auto=True)}\n"
        )
    return msg + "\n"


def process_wallet(config, history, today_iso):
    address = config["address"]
    today_label = fmt_date(today_iso)

    header_link = f'<a href="{config["link"]}">{config["name"]}</a>'
    parts = [header_link]
    if config["description"]:
        parts.append(f'<i>{config["description"]}</i>')
    parts.append(today_label)
    header = " - ".join(parts) + "\n"  # bez pustej linii przed Account Equity

    data = get_hyperliquid_data(address)
    if data is None:
        send_telegram_message(header + "\n<i>Nie udało się pobrać danych portfela.</i>")
        return

    account_equity, all_positions = data

    prev_date, prev_snap = get_previous_snapshot(history, address, today_iso)
    prev_label = fmt_date(prev_date) if prev_date else None
    prev_positions = prev_snap["positions"] if prev_snap else {}

    msg = header

    # Account Equity (tuż pod nagłówkiem)
    if prev_snap:
        msg += (
            f"Account Equity: <b>{fmt_usd(account_equity, 2)}</b> "
            f"(zmiana {fmt_change_pct(prev_snap['account_value'], account_equity)})\n\n"
        )
    else:
        msg += f"Account Equity: <b>{fmt_usd(account_equity, 2)}</b>\n\n"

    top5 = all_positions[:5]

    if not top5:
        msg += "<i>Brak aktywnych pozycji na portfelu.</i>\n\n"

    for p in top5:
        prev_pos = prev_positions.get(p["coin"])
        # zmiana kierunku = traktujemy jako nową pozycję
        if prev_pos and prev_pos["direction"] != p["direction"]:
            prev_pos = None
        if prev_snap is None:
            msg += build_position_block_no_history(p)
        else:
            msg += build_position_block(p, prev_pos, prev_label)

    # Pozycje zamknięte od poprzedniego zestawienia
    if prev_snap:
        current = {p["coin"]: p for p in all_positions}
        closed = [
            c for c, pp in prev_positions.items()
            if c not in current or current[c]["direction"] != pp["direction"]
        ]
        if closed:
            lines = ", ".join(
                f"{c} {prev_positions[c]['direction']} ({fmt_usd(prev_positions[c]['val'], 2)})"
                for c in closed
            )
            msg += f"<b>Zamknięte od {prev_label}:</b> {lines}\n"
    else:
        msg += "<i>Brak danych historycznych. Porównanie będzie dostępne od kolejnego uruchomienia.</i>\n"

    send_telegram_message(msg.rstrip() + "\n")

    # Zapis dzisiejszego snapshotu (nadpisuje poprzedni zapis z tej samej daty)
    history.setdefault(address, {})[today_iso] = {
        "account_value": account_equity,
        "positions": {
            p["coin"]: {
                "direction": p["direction"],
                "val": p["val"],
                "upnl": p["upnl"],
                "pnl_pct": p["pnl_pct"],
                "entry_px": p["entry_px"],
                "mark_px": p["mark_px"],
            }
            for p in all_positions
        },
    }


def main():
    wallets = load_wallet_configs("01_Wallet_Address.txt")
    if not wallets:
        print("Brak poprawnych konfiguracji w pliku 01_Wallet_Address.txt")
        return

    today_iso = datetime.now(TZ).strftime("%Y-%m-%d")
    history = load_history()

    for wallet in wallets:
        process_wallet(wallet, history, today_iso)

    save_history(history)


if __name__ == "__main__":
    main()
