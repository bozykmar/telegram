import os
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

API_URL = "https://api.hyperliquid.xyz/info"
HEADERS = {"Content-Type": "application/json"}

def load_wallet_configs(file_path="01_Wallet_Address.txt"):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Nie znaleziono pliku {file_path}")

    wallets = []
    with open(file_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    for line in lines:
        parts = [p.strip() for p in line.split("-") if p.strip()]
        link = parts[1] if len(parts) > 1 else ""
        wallet_address = ""
        if "/trader/" in link:
            wallet_address = link.split("/trader/")[-1].strip()

        if wallet_address:
            wallets.append({
                "name": parts[0] if len(parts) > 0 else "Wallet",
                "link": link,
                "description": parts[2] if len(parts) > 2 else "",
                "address": wallet_address
            })

    return wallets

def get_hyperliquid_positions(address):
    positions = []
    try:
        payload = {"type": "clearinghouseState", "user": address}
        res_perp = requests.post(API_URL, json=payload, headers=HEADERS).json()
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
        print(f"Błąd pobierania danych dla {address}: {e}")
            
    positions.sort(key=lambda x: x["val"], reverse=True)
    return positions[:5]

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    res = requests.post(url, json=payload)
    if not res.ok:
        print(f"Błąd Telegrama: {res.text}")
        res.raise_for_status()

def process_wallet(config):
    top5 = get_hyperliquid_positions(config["address"])
    
    header_link = f'<a href="{config["link"]}">{config["name"]}</a>'
    if config["description"]:
        header = f'{header_link} - <i>{config["description"]}</i>\n\n'
    else:
        header = f'{header_link}\n\n'

    msg = header

    if not top5:
        msg += "<i>Brak aktywnych pozycji na portfelu.</i>"
        send_telegram_message(msg)
        return

    for i, p in enumerate(top5, 1):
        pnl_sign = "+" if p["upnl"] >= 0 else ""
        val_str = f"${p['val']:,.2f}"
        upnl_str = f"{pnl_sign}${p['upnl']:,.2f}"
        pct_str = f"{pnl_sign}{p['pnl_pct']:.2f}%"
        avg_str = f"${p['entry_px']:,.4f}"
        
        msg += f"<b>{i}. {p['coin']}</b> | {p['color_icon']} <b>{p['direction']}</b> <b>{p['leverage']}</b>\n"
        msg += f"   Position: <b>{val_str}</b>\n"
        msg += f"   uPnL: <b>{upnl_str}</b> (<b>{pct_str}</b>)\n"
        msg += f"   Avg. Price: <b>{avg_str}</b>\n\n"

    send_telegram_message(msg)

def main():
    wallets = load_wallet_configs("01_Wallet_Address.txt")
    if not wallets:
        print("Brak poprawnych konfiguracji w pliku 01_Wallet_Address.txt")
        return

    for wallet in wallets:
        process_wallet(wallet)

if __name__ == "__main__":
    main()
