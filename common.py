"""
common.py

Wspólne narzędzia skryptów 00-04: konfiguracja (zmienne środowiskowe), HTTP z ponawianiem,
wysyłka na Telegram (podział długich wiadomości, ponawianie, limit 429), formatowanie kwot,
odczyt/zapis plików JSON oraz pobieranie portfeli z Hyperliquid.

Skrypty importują ten plik (import common) - uruchamiaj je z katalogu repozytorium.
Nie ma tu żadnej logiki biznesowej poszczególnych raportów.
"""

import os
import re
import sys
import json
import time
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TZ = ZoneInfo("Europe/Warsaw")
NBSP = "\u00a0"                  # twarda spacja, żeby kwota nie łamała się w nowej linii
HTTP_TIMEOUT = 30
TELEGRAM_MAX_LEN = 4000          # limit Telegrama to 4096 znaków; zapas na bezpieczeństwo
TELEGRAM_ATTEMPTS = 3
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
HYPERBOT_URL = "https://hyperbot.network/trader/"
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")


# ---------------------------------------------------------------- konfiguracja

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


# ---------------------------------------------------------------- HTTP

def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


SESSION = make_session()


def http_get(url, params=None, retries=3, user_agent="telegram-bots"):
    """GET -> JSON. Błędów klienta (4xx poza 429) nie ponawia."""
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                             headers={"User-Agent": user_agent})
            if r.status_code >= 400:
                err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]!r}")
                if r.status_code < 500 and r.status_code != 429:
                    raise err
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


# ---------------------------------------------------------------- formatowanie

def fmt_usd(value, decimals=2, signed=False):
    """
    >= 1000 USD: bez miejsc po przecinku, twarda spacja jako separator tysięcy ($2 129).
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


# ---------------------------------------------------------------- JSON

def load_json(path, default=None):
    """Wczytuje JSON; przy braku lub uszkodzeniu pliku zwraca `default` (domyślnie {})."""
    default = {} if default is None else default
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (json.JSONDecodeError, OSError) as e:
        print(f"Nie udało się wczytać {path}: {e}")
        return default


def save_json(path, data, indent=1):
    """Zapis atomowy (plik tymczasowy + os.replace)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


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


class Telegram:
    """Wysyłka na Telegram do czatu z TELEGRAM_CHAT_ID i (opcjonalnie) tematu z `thread_env`."""

    def __init__(self, thread_env):
        self.token = env("TELEGRAM_TOKEN")
        self.chat_id = env("TELEGRAM_CHAT_ID")
        self.thread_id = env_int(thread_env)

    def require(self):
        if not (self.token and self.chat_id):
            sys.exit("Brak TELEGRAM_TOKEN lub TELEGRAM_CHAT_ID w zmiennych środowiskowych.")

    def _request(self, method, payload, file_field=None, file_path=None):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        payload = dict(payload, chat_id=self.chat_id)
        if self.thread_id is not None:
            payload["message_thread_id"] = self.thread_id
        print(f"Telegram: temat = {self.thread_id if self.thread_id is not None else 'BRAK (wiadomość trafi do General)'}")

        last_error = None
        for attempt in range(TELEGRAM_ATTEMPTS):
            try:
                if file_path:
                    with open(file_path, "rb") as f:
                        res = requests.post(url, data=payload, files={file_field: f},
                                            timeout=HTTP_TIMEOUT * 2)
                else:
                    res = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
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

        raise last_error or RuntimeError("Nie udało się wysłać na Telegram")

    def send_message(self, text):
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            self._request("sendMessage", {"text": chunk, "parse_mode": "HTML",
                                          "disable_web_page_preview": True})
            if i < len(chunks) - 1:
                time.sleep(1.1)

    def send_photo(self, path, caption):
        self._request("sendPhoto", {"caption": caption, "parse_mode": "HTML"},
                      file_field="photo", file_path=path)


# ---------------------------------------------------------------- portfele / Hyperliquid

def load_wallet_configs(file_path):
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


def get_hyperliquid_data(address):
    """
    Zwraca (account_equity, lista_pozycji) lub None przy błędzie.
    Udział pozycji = margin pozycji / suma marginów wszystkich otwartych pozycji.
    """
    positions = []
    try:
        res = SESSION.post(HYPERLIQUID_URL, json={"type": "clearinghouseState", "user": address},
                           timeout=HTTP_TIMEOUT)
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
