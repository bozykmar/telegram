import os
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def wyslij_telegram(tekst):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": tekst, "parse_mode": "HTML"}
    response = requests.post(url, json=payload)
    response.raise_for_status()

wyslij_telegram("🚀 <b>Test z GitHub Actions:</b> Skrypt działa!")
