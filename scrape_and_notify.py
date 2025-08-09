#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
İZKO 'Güncel Kur' sayfasındaki 'Cumhuriyet' satırındaki 'YENİ' fiyatı izler.
- Önce BeautifulSoup ile DOM üzerinden çekmeye çalışır.
- Olmazsa regex fallback uygular: r"Cumhuriyet[\s\S]{0,120}?YENİ\s*:?\s*([0-9\.,]+)"
- Durumu state/last_price.json dosyasında tutar.
- Değişim varsa TELEGRAM mesajı yollar.
- İlk çalıştırmada baseline oluşturur, bildirim göndermez.
- Ağ isteklerinde 10 sn timeout ve 3 deneme (exponential backoff) vardır.
- Loglar print() ile yazılır; ayrıştırma başarısız olsa bile exit code 0 (workflow kırılmaz).

Bağımlılıklar: requests, bs4, pytz
Python: 3.11+
"""

import os
import json
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytz
import requests
from bs4 import BeautifulSoup

URL = "https://www.izko.org.tr/Home/GuncelKur"
STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "last_price.json"

# Regex fallback (case-insensitive)
FALLBACK_REGEX = re.compile(
    r"Cumhuriyet[\s\S]{0,4000}?YENİ\s*:?\s*([0-9\.,]+)", re.IGNORECASE
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}

def fetch_html(url: str, max_retries: int = 3, timeout: int = 10) -> str | None:
    """Sayfayı 10 sn timeout ve 3 denemeye kadar (1s,2s,4s backoff) indirir."""
    session = requests.Session()
    backoff = 1
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200 and resp.text:
                print(f"[INFO] GET {url} -> 200 (attempt {attempt})")
                return resp.text
            else:
                print(f"[WARN] GET {url} -> {resp.status_code} (attempt {attempt})")
        except requests.RequestException as e:
            print(f"[WARN] Request error (attempt {attempt}): {e}")
        if attempt < max_retries:
            time.sleep(backoff)
            backoff *= 2
    print("[ERROR] HTML alınamadı; tüm denemeler tükendi.")
    return None

def _turkish_number_to_decimal(num_text: str) -> Decimal | None:
    """
    '29.650,00' veya '29650' gibi formatları Decimal'e çevirir:
    - tüm noktaları siler, virgülü ondalığa çevirir.
    """
    cleaned = num_text.strip()
    cleaned = cleaned.replace("\xa0", " ").replace(" ", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None

def _format_tl(n: Decimal | int | float) -> str:
    """Mesajda TL'yi binlik '.' ile göster (örn. 29.650)."""
    try:
        as_int = int(Decimal(n).quantize(Decimal("1")))
    except Exception:
        as_int = int(float(n))
    return f"{as_int:,}".replace(",", ".")

def parse_price_with_bs4(html: str) -> Decimal | None:
    """DOM üzerinden 'Cumhuriyet' ve bağlamında 'YENİ' değerini bul."""
    soup = BeautifulSoup(html, "html.parser")

    candidates = soup.find_all(string=re.compile(r"Cumhuriyet", re.IGNORECASE))
    for text_node in candidates:
        row = None
        if text_node and getattr(text_node, "parent", None):
            row = text_node.find_parent("tr")
        container = row or (text_node.parent if text_node else None)
        if not container:
            continue

        ctx_text = container.get_text(" ", strip=True)
        m = re.search(r"YENİ\s*:?[\s]*([0-9\.,]+)", ctx_text, re.IGNORECASE)
        if m:
            val = _turkish_number_to_decimal(m.group(1))
            if val is not None:
                print(f"[INFO] BS4 ile bulundu: {m.group(1)} -> {val}")
                return val

        parent = container.find_parent() if hasattr(container, "find_parent") else None
        if parent:
            ptxt = parent.get_text(" ", strip=True)
            m2 = re.search(r"YENİ\s*:?[\s]*([0-9\.,]+)", ptxt, re.IGNORECASE)
            if m2:
                val2 = _turkish_number_to_decimal(m2.group(1))
                if val2 is not None:
                    print(f"[INFO] BS4 (ebeveyn) ile bulundu: {m2.group(1)} -> {val2}")
                    return val2

    print("[WARN] BS4 ile fiyat bulunamadı; regex fallback denenecek.")
    return None

def parse_price_with_regex(html: str) -> Decimal | None:
    """Fallback: HTML tüm metni üzerinde verilen regex ile ara."""
    m = FALLBACK_REGEX.search(html)
    if not m:
        print("[ERROR] Regex ile fiyat yakalanamadı.")
        return None
    num_txt = m.group(1)
    val = _turkish_number_to_decimal(num_txt)
    if val is None:
        print(f"[ERROR] Regex sayı parse edilemedi: '{num_txt}'")
        return None
    print(f"[INFO] Regex ile bulundu: {num_txt} -> {val}")
    return val

def parse_price_neighborhood(html: str) -> Decimal | None:
    """
    Komşuluk araması: 'Cumhuriyet' görülen her pozisyonda, sonraki 2000 karakter içinde
    'YENİ' ve onu izleyen sayı desenini yakalamaya çalışır.
    """
    for m in re.finditer(r"Cumhuriyet", html, flags=re.IGNORECASE):
        start = m.start()
        window = html[start : start + 2000]  # ileriye doğru bak
        # 'YENİ' yi ve onu izleyen sayıyı ara:
        m2 = re.search(r"YENİ[\s:]*([0-9\.\,\s]+)", window, flags=re.IGNORECASE)
        if m2:
            num_txt = m2.group(1)
            val = _turkish_number_to_decimal(num_txt)
            if val is not None:
                print(f"[INFO] Neighborhood ile bulundu: {num_txt} -> {val}")
                return val

    # Debug: ilk 'Cumhuriyet' etrafını logla (regex tutmadıysa yapıyı görürüz)
    m_first = re.search(r"Cumhuriyet", html, flags=re.IGNORECASE)
    if m_first:
        i = m_first.start()
        lo = max(0, i - 150)
        hi = min(len(html), i + 400)
        context = html[lo:hi]
        print("[DEBUG] 'Cumhuriyet' çevresi (≈300 chars):")
        print(context.replace("\n", " ")[:300])
    return None

def load_last_price() -> Decimal | None:
    """Önceki fiyatı state dosyasından okur; yoksa None döner."""
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
    """Son fiyatı state dosyasına yazar (klasörü oluşturarak)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_price": int(price.quantize(Decimal("1"))),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[INFO] last_price.json güncellendi: {payload}")

def _istanbul_now_str() -> tuple[str, str]:
    """Europe/Istanbul tarih-saat stringi ve +HH:MM ofsetini döndürür."""
    tz = pytz.timezone("Europe/Istanbul")
    now = datetime.now(tz)
    offset = now.utcoffset()
    total_minutes = int((offset.total_seconds() if offset else 3 * 3600) // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hh = total_minutes // 60
    mm = total_minutes % 60
    offset_str = f"{sign}{hh:02d}:{mm:02d}"
    return now.strftime("%Y-%m-%d %H:%M:%S"), offset_str

def build_message(old_price: Decimal, new_price: Decimal) -> str:
    """İstenen metin formatında Telegram mesajı üretir."""
    dt_str, offset = _istanbul_now_str()
    msg = (
        "İZKO Cumhuriyet Altını fiyatı değişti!\n"
        f"Eski: {_format_tl(old_price)} TL\n"
        f"Yeni: {_format_tl(new_price)} TL\n"
        "Kaynak: izko.org.tr\n"
        f"Zaman: {dt_str} ({offset})"
    )
    return msg

def notify_telegram(message: str) -> None:
    """Telegram Bot API ile mesaj gönderir (POST)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[INFO] Telegram secrets eksik; Telegram bildirimi atlanıyor.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            print("[INFO] Telegram bildirimi gönderildi.")
        else:
            print(f"[WARN] Telegram status {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[WARN] Telegram isteği hata verdi: {e}")

def main() -> int:
    html = fetch_html(URL)
    if not html:
        print("[ERROR] HTML alınamadığı için işlenemedi. Exit 0 (workflow kırılmasın).")
        return 0

    # 1) Önce BeautifulSoup ile dene
    price = parse_price_with_bs4(html)

    # 2) Olmazsa regex fallback
    if price is None:
        price = parse_price_with_regex(html)

    # 3) Hâlâ yoksa komşuluk yaklaşımı
    if price is None:
        price = parse_price_neighborhood(html)

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
        message = build_message(last, price)
        notify_telegram(message)
        save_last_price(price)
    else:
        print(f"[INFO] Değişim yok. Güncel fiyat: {_format_tl(price)} TL")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

