#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
İZKO 'Güncel Kur' sayfasında 'Cumhuriyet' SATIŞ fiyatını **JS sonrası DOM'dan** okur.
- Headless Chromium (Playwright) ile sayfayı render eder.
- Önce '#row7_satis #ataLabel' seçicisinden okur.
- Olmazsa 'tr:has-text("Cumhuriyet")' satırındaki ilk makul sayıyı alır.
- Son fiyatı state/last_price.json içinde tutar; değişirse Telegram'a mesaj atar.
- İlk çalıştırmada baseline oluşturur, bildirim göndermez.
- Log'lar print() ile GitHub Actions'da görünür.

Gereken Secrets:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pytz
import requests

URL = "https://www.izko.org.tr/Home/GuncelKur"
STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "last_price.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}

def _turkish_number_to_decimal(txt: str) -> Decimal | None:
    if not txt:
        return None
    cleaned = txt.strip().replace("\xa0", " ").replace(" ", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None

def _format_tl(n: Decimal | int | float) -> str:
    try:
        val = int(Decimal(n).quantize(Decimal("1")))
    except Exception:
        val = int(float(n))
    return f"{val:,}".replace(",", ".")

def _istanbul_now_str() -> tuple[str, str]:
    tz = pytz.timezone("Europe/Istanbul")
    now = datetime.now(tz)
    offset = now.utcoffset()
    total_minutes = int((offset.total_seconds() if offset else 3 * 3600) // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hh, mm = divmod(total_minutes, 60)
    return now.strftime("%Y-%m-%d %H:%M:%S"), f"{sign}{hh:02d}:{mm:02d}"

def build_message(old_price: Decimal, new_price: Decimal) -> str:
    dt_str, offset = _istanbul_now_str()
    return (
        "İZKO Cumhuriyet Altını fiyatı değişti!\n"
        f"Eski: {_format_tl(old_price)} TL\n"
        f"Yeni: {_format_tl(new_price)} TL\n"
        "Kaynak: izko.org.tr\n"
        f"Zaman: {dt_str} ({offset})"
    )

def notify_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[INFO] Telegram secrets eksik; bildirim atlanıyor.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": message}, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            print("[INFO] Telegram bildirimi gönderildi.")
        else:
            print(f"[WARN] Telegram status {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[WARN] Telegram isteği hata verdi: {e}")

def load_last_price() -> Decimal | None:
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        lp = data.get("last_price")
        return Decimal(str(lp)) if lp is not None else None
    except Exception as e:
        print(f"[WARN] last_price.json okunamadı: {e}")
        return None

def save_last_price(price: Decimal) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_price": int(price.quantize(Decimal("1"))),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[INFO] last_price.json güncellendi: {payload}")

def get_price_via_headless_dom(url: str) -> Decimal | None:
    """
    JS sonrası DOM'dan 30410 gibi fiyatı okur.
    1) '#row7_satis #ataLabel' -> textContent
    2) Olmazsa: 'tr:has-text("Cumhuriyet")' satırındaki ilk makul sayı
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[ERROR] Playwright import edilemedi: {e}")
        return None

    # 3 deneme, küçük backoff ile
    for attempt in range(1, 4):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(locale="tr-TR", timezone_id="Europe/Istanbul")
                page = context.new_page(viewport={"width": 1280, "height": 1600})
                page.goto(url, timeout=30000, wait_until="networkidle")

                # 1) Doğrudan hedef seçici
                try:
                    page.wait_for_selector("#row7_satis #ataLabel", timeout=5000)
                    txt = page.eval_on_selector("#row7_satis #ataLabel", "el => (el.textContent || '').trim()")
                except Exception:
                    txt = ""

                # 2) Satırdan yakala (Cumhuriyet satırı)
                if not txt:
                    row = page.locator("tr:has-text('Cumhuriyet')").first
                    if row and row.count() >= 0:
                        try:
                            row.wait_for(timeout=3000)
                        except Exception:
                            pass
                        row_text = row.inner_text(timeout=5000) if row else ""
                        # Satırdaki tüm sayılardan makul olanı seç (>= 1000)
                        m = re.search(r"([0-9][0-9\.\,\s]{2,})", row_text)
                        txt = m.group(1) if m else ""

                browser.close()

                if not txt:
                    print(f"[WARN] Headless DOM denemesi (attempt {attempt}): metin boş.")
                else:
                    val = _turkish_number_to_decimal(txt)
                    if val and val >= Decimal(1000):
                        print(f"[INFO] Headless DOM ile bulundu: {val}")
                        return val
                    else:
                        print(f"[WARN] Headless DOM sayı makul değil: '{txt}' (attempt {attempt})")

        except Exception as e:
            print(f"[WARN] Headless DOM hata (attempt {attempt}): {e}")

    print("[ERROR] Headless DOM ile fiyat bulunamadı.")
    return None

def main() -> int:
    # 1) Headless DOM yöntemi (asıl yol)
    price = get_price_via_headless_dom(URL)

    if price is None:
        print("[ERROR] Fiyat ayrıştırılamadı. Exit 0 (workflow kırılmasın).")
        return 0

    price = price.quantize(Decimal("1"))

    last = load_last_price()
    if last is None:
        print(f"[INFO] İlk tespit (baseline): {_format_tl(price)} TL. Bildirim YOK.")
        save_last_price(price)
        return 0

    if price != last:
        print(f"[INFO] Değişim tespit edildi: {_format_tl(last)} -> {_format_tl(price)} TL")
        notify_telegram(build_message(last, price))
        save_last_price(price)
    else:
        print(f"[INFO] Değişim yok. Güncel fiyat: {_format_tl(price)} TL")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
