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
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, re, time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytz
import requests
from bs4 import BeautifulSoup

URL = "https://www.izko.org.tr/Home/GuncelKur"
STATE_DIR = Path("state")
STATE_FILE = STATE_DIR / "last_price.json"

# Daha geniş pencere + sayı grubunda boşlukları da kabul et
FALLBACK_REGEX = re.compile(
    r"Cumhuriyet[\s\S]{0,4000}?YEN[İI][\s:]*([0-9\.\,\s]+)", re.IGNORECASE
)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
}

def fetch_html(url: str, max_retries: int = 3, timeout: int = 10) -> str | None:
    session = requests.Session()
    backoff = 1
    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200 and r.text:
                print(f"[INFO] GET {url} -> 200 (attempt {attempt})")
                return r.text
            else:
                print(f"[WARN] GET {url} -> {r.status_code} (attempt {attempt})")
        except requests.RequestException as e:
            print(f"[WARN] Request error (attempt {attempt}): {e}")
        if attempt < max_retries:
            time.sleep(backoff); backoff *= 2
    print("[ERROR] HTML alınamadı; tüm denemeler tükendi.")
    return None

def _turkish_number_to_decimal(num_text: str) -> Decimal | None:
    cleaned = (num_text or "").strip()
    cleaned = cleaned.replace("\xa0", " ").replace(" ", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None

def _format_tl(n: Decimal | int | float) -> str:
    try:
        as_int = int(Decimal(n).quantize(Decimal("1")))
    except Exception:
        as_int = int(float(n))
    return f"{as_int:,}".replace(",", ".")

def parse_price_with_bs4(html: str) -> Decimal | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.find_all(string=re.compile(r"Cumhuriyet", re.IGNORECASE))
    for text_node in candidates:
        row = text_node.find_parent("tr") if getattr(text_node, "find_parent", None) else None
        container = row or (text_node.parent if text_node else None)
        if not container:
            continue

        # 1) satır içinde ara
        ctx_text = container.get_text(" ", strip=True)
        m = re.search(r"YEN[İI][\s:]*([0-9\.\,\s]+)", ctx_text, re.IGNORECASE)
        if m:
            val = _turkish_number_to_decimal(m.group(1))
            if val is not None:
                print(f"[INFO] BS4 ile bulundu: {m.group(1)} -> {val}")
                return val

        # 2) ebeveyn içinde ara (layout farklı olabilir)
        parent = container.find_parent() if hasattr(container, "find_parent") else None
        if parent:
            ptxt = parent.get_text(" ", strip=True)
            m2 = re.search(r"YEN[İI][\s:]*([0-9\.\,\s]+)", ptxt, re.IGNORECASE)
            if m2:
                val2 = _turkish_number_to_decimal(m2.group(1))
                if val2 is not None:
                    print(f"[INFO] BS4 (ebeveyn) ile bulundu: {m2.group(1)} -> {val2}")
                    return val2

    print("[WARN] BS4 ile fiyat bulunamadı; regex fallback denenecek.")
    return None

def parse_price_with_regex(html: str) -> Decimal | None:
    m = FALLBACK_REGEX.search(html)
    if not m:
        print("[ERROR] Regex ile fiyat yakalanamadı.")
        return None
    val = _turkish_number_to_decimal(m.group(1))
    if val is None:
        print(f"[ERROR] Regex sayı parse edilemedi: '{m.group(1)}'")
        return None
    print(f"[INFO] Regex ile bulundu: {m.group(1)} -> {val}")
    return val

def parse_price_neighborhood(html: str) -> Decimal | None:
    """
    'Cumhuriyet' görülen her noktadan sonra 4000 karakter pencere aç,
    önce 'YENİ'yi bul, onun 400 karakter sonrasında ilk sayı desenini al.
    """
    for m in re.finditer(r"Cumhuriyet", html, flags=re.IGNORECASE):
        start = m.start()
        window = html[start : start + 4000]
        yen = re.search(r"YEN[İI]", window, flags=re.IGNORECASE)
        if not yen:
            continue
        after = window[yen.end(): yen.end()+400]
        num = re.search(r"([0-9][0-9\.\,\s]{2,})", after)
        if num:
            val = _turkish_number_to_decimal(num.group(1))
            if val is not None:
                print(f"[INFO] Neighborhood ile bulundu: {num.group(1)} -> {val}")
                return val

    # DEBUG: ilk 'Cumhuriyet' çevresini logla
    m_first = re.search(r"Cumhuriyet", html, flags=re.IGNORECASE)
    if m_first:
        i = m_first.start(); lo = max(0, i-150); hi = min(len(html), i+450)
        context = html[lo:hi].replace("\n", " ")
        print("[DEBUG] 'Cumhuriyet' çevresi (~300 chars):")
        print(context[:300])
    return None

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

def _istanbul_now_str() -> tuple[str, str]:
    tz = pytz.timezone("Europe/Istanbul")
    now = datetime.now(tz)
    offset = now.utcoffset()
    total_minutes = int((offset.total_seconds() if offset else 3*3600) // 60)
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
        print("[INFO] Telegram secrets eksik; Telegram bildirimi atlanıyor.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": message},
                          headers=HEADERS, timeout=10)
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

    # 1) BS4 → 2) Regex → 3) Komşuluk
    price = parse_price_with_bs4(html)
    if price is None:
        price = parse_price_with_regex(html)
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
        notify_telegram(build_message(last, price))
        save_last_price(price)
    else:
        print(f"[INFO] Değişim yok. Güncel fiyat: {_format_tl(price)} TL")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
