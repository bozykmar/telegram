import os
import re
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

API_URL = "https://api.hyperliquid.xyz/info"
HEADERS = {"Content-Type": "application/json"}

def load_wallet_config(file_path="01_Wallet_Address.txt"):
    """
    Wczytuje konfigurację portfela z pliku tekstowego.
    Format pliku: Nazwa - Link - Opis
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Nie znaleziono pliku {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        line = f.read().strip()

    parts = [p.strip() for p in line.split("-") if p.strip()]
    
    # Wyciąganie adresu z linku (zakładamy format .../trader/0x...)
    link = parts[1] if len(parts) > 1 else ""
    wallet_address = ""
    if "/trader/" in link:
        wallet_address = link.split("/trader/")[-1].strip()

    return {
        "name": parts[0] if len(parts) > 0 else "Wallet",
        "link": link,
        "description": parts[2] if len(parts) > 2 else "",
        "address": wallet_address
    }

def escape_markdown(text):
    """Eskapuje znaki specjalne wymagane przez Telegram MarkdownV2."""
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(r'([%s])' % re.escape(escape_chars), r'\\\1', text)

def get_spot_token_names():
    """Pobiera mapowanie ID tokenów spot/HIP-1 na ich nazwy rynkowe."""
    try:
        res = requests.post(API_URL, json={"type": "spotMeta"}, headers=HEADERS).json()
        tokens = res.get("tokens", [])
        return {idx: token.get("name") for idx, token in enumerate(tokens)}
    except Exception as e:
        print(f"Błąd pobierania spotMeta: {e}")
        return {}

def get_hyperliquid_positions(address):
    positions = []
    token_map = get_spot_token_names()

    # 1. Pobieranie pozycji PERPETUAL (Krypto + HIP-1 perps jak xyz:BRENTOIL)
    try:
        res_perp = requests.post(API_URL, json={"type": "clearinghouseState", "user": address}, headers=HEADERS).json()
        asset_positions = res_perp.get("assetPositions", [])
        
        for item in asset_positions:
            pos = item.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi != 0:
                coin = pos.get("coin")
                entry_px = float(pos.get("entryPx", 0))
                position_value = float(pos.get("positionValue", 0))
                upnl = float(pos.get("unrealizedPnl", 0))
                direction = "LONG" if szi > 0 else "SHORT"
                color_icon = "🟢" if szi > 0 else "🔴"
                
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
                    "val": position_value,
                    "upnl": upnl,
                    "pnl_pct": pnl_pct,
                    "leverage": leverage_str
                })
    except Exception as e:
        print(f"Błąd pobierania pozycji Perp: {e}")

    # 2. Pobieranie pozycji SPOT / HIP-1
    try:
        res_spot = requests.post(API_URL, json={"type": "spotClearinghouseState", "user": address}, headers=HEADERS).json()
        balances = res_spot.get("balances", [])
        
        for b in balances:
            coin_idx = b.get("token")
            coin_name = token_map.get(coin_idx, b.get("coin", "UNKNOWN"))
            
            total_sz = float(b.get("total", 0))
            entry_px = float(b.get("entryPx", 0)) if b.get("entryPx") else 0.0
            
            if total_sz > 0 and coin_name not in ["USDC", "USDT"]:
                val = float(b.get("entryPx", 0)) * total_sz if entry_px > 0 else total_sz
                display_name = f"xyz:{coin_name}" if not coin_name.startswith("xyz:") else coin_name
                
                positions.append({
                    "coin": display_name,
                    "direction": "SPOT",
                    "color_icon": "⚪",
                    "entry_px": entry_px,
                    "val": val,
                    "upnl": 0.0,
                    "pnl_pct": 0.0,
                    "leverage": "1x"
                })
    except Exception as e:
        print(f"Błąd pobierania pozycji Spot: {e}")
            
    # Sortowanie pozycji malejąco po wartości pozycji (positionValue)
    positions.sort(key=lambda x: x["val"], reverse=True)
    return positions[:5]

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True
    }
    res = requests.post(url, json=payload)
    if not res.ok:
        print(f"Błąd wysyłania do Telegrama: {res.text}")

def main():
    config = load_wallet_config("01_Wallet_Address.txt")
    
    if not config["address"]:
        print("Nie udało się odczytać adresu portfela z pliku 01_Wallet_Address.txt")
        return

    top5 = get_hyperliquid_positions(config["address"])
    
    if not top5:
        send_telegram_message("Brak aktywnych pozycji na portfelu.")
        return

    # Budowanie nagłówka wiadomości
    header_link = f"[{escape_markdown(config['name'])}]({config['link']})"
    if config["description"]:
        header = f"{header_link} \\- _{escape_markdown(config['description'])}_\n\n"
    else:
        header = f"{header_link}\n\n"

    msg = header
    
    for i, p in enumerate(top5, 1):
        pnl_sign = "+" if p["upnl"] >= 0 else ""
        
        val_str = f"${p['val']:,.2f}"
        upnl_str = f"{pnl_sign}${p['upnl']:,.2f}"
        pct_str = f"{pnl_sign}{p['pnl_pct']:.2f}%"
        avg_str = f"${p['entry_px']:,.4f}"
        
        msg += f"*{i}\\. {escape_markdown(p['coin'])}* | {p['color_icon']} *{escape_markdown(p['direction'])}* *{escape_markdown(p['leverage'])}*\n"
        msg += f"   Position: *{escape_markdown(val_str)}*\n"
        
        if p["direction"] != "SPOT":
            msg += f"   uPnL: *{escape_markdown(upnl_str)}* (*{escape_markdown(pct_str)}*)\n"
        
        msg += f"   Avg. Price: *{escape_markdown(avg_str)}*\n\n"

    send_telegram_message(msg)

if __name__ == "__main__":
    main()
