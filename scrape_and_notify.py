#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
İZKO 'Güncel Kur' sayfasındaki 'Cumhuriyet' satırındaki 'YENİ' fiyatı izler.
- Önce BeautifulSoup ile DOM üzerinden çekmeye çalışır.
- Olmazsa regex fallback uygular: r"Cumhuriyet[\s\S]{0,4000}?YENİ\s*:?\s*([0-9\.,]+)"
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

def parse_price_via_table(html: str) -> Decimal | None:
    """
    Tablo tabanlı ayrıştırma:
    - Sayfadaki ilk/uygun <table> içinde thead/th başlıkları ara.
    - 'Cumhuriyet' yazan satırı (tr) bul.
    - Başlıklarda 'YENİ' hangi sütundaysa o hücrenin (td) metnini sayıya çevir.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) Tabloları sırayla dene
    for table in soup.find_all("table"):
        # Başlıkları topla
        headers = []
        thead = table.find("thead")
        if thead:
            ths = thead.find_all(["th", "td"])
            headers = [th.get_text(" ", strip=True) for th in ths]

        # thead yoksa ilk satırı başlık kabul etmeyi deneyelim
        if not headers:
            first_tr = table.find("tr")
            if first_tr:
                headers = [c.get_text(" ", strip=True) for c in first_tr.find_all(["th","td"])]

        if not headers:
            continue

        # 'YENİ' sütun index'ini bul (toleranslı)
        yeni_idx = None
        for i, h in enumerate(headers):
            ht = (h or "").strip().upper()
            if "YEN" in ht:  # 'YENİ' / 'YENI' / 'YEN ' vb.
                yeni_idx = i
                break
        if yeni_idx is None:
            continue

        # 2) Gövdede 'Cumhuriyet' geçen satırı bul
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            row_text = tr.get_text(" ", strip=True)
            if not re.search(r"Cumhuriyet", row_text, flags=re.IGNORECASE):
                continue

            # Satırdaki hücreleri al
            cells = tr.find_all(["td", "th"])
            if not cells or yeni_idx >= len(cells):
                continue

            # 'YENİ' sütunundaki ham metin
            yeni_cell = cells[yeni_idx]
            # Hücre içinde ikon/span vs. olabilir; düz metni al
            raw = yeni_cell.get_text(" ", strip=True)
            # Eğer metin boşsa, içerikteki 'data-*' attribute’larına da bak (bazı siteler sayıyı attribute’ta tutar)
            if not raw:
                for attr, val in yeni_cell.attrs.items():
                    if isinstance(val, str) and re.search(r"[0-9]", val):
                        raw = val
                        break

            # Hücre içinden sayı çek (esnek)
            m = re.search(r"([0-9][0-9\.\,\s]{1,})", raw)
            num_txt = m.group(1) if m else raw

            val = _turkish_number_to_decimal(num_txt)
            if val is not None and val >= Decimal(1000):
                print(f"[INFO] Tabloyla bulundu (YENİ): {num_txt} -> {val}")
                return val

    print("[WARN] Tablo tabanlı ayrıştırma başarısız.")
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
    'Cumhuriyet' görülen her noktadan sonra bir pencere alır,
    önce pencereyi HTML'den arındırıp düz metin yapar, sonra
    'YENİ' yakınındaki gerçek sayıyı seçer.
    """
    for m in re.finditer(r"Cumhuriyet", html, flags=re.IGNORECASE):
        start = m.start()
        window_html = html[start : start + 5000]  # geniş pencere

        # 1) Pencereyi düz METNE çevir (tag'leri at)
        window_text = BeautifulSoup(window_html, "html.parser").get_text(" ", strip=True)

        # 2) 'YENİ' den sonra ilk "sayı blok"u yakala (boşluk/nokta/virgül serbest)
        m2 = re.search(r"YEN[İI]\s*[:\-]?\s*([0-9\.\,\s]{3,})", window_text, flags=re.IGNORECASE)
        candidates = []
        if m2:
            candidates.append(m2.group(1))

        # 3) Yedek: penceredeki TÜM sayı benzeri blokları topla (4–7 hane civarı)
        candidates += re.findall(r"([0-9][0-9\.\,\s]{2,})", window_text)

        # 4) Adayları sayıya çevir, 0 yığınlarını ve çok küçükleri ele, en büyük mantıklı olanı seç
        parsed = []
        for c in candidates:
            c_clean = c.replace("\xa0", " ").strip()
            # "0 0 0 0" gibi saçmalıkları at
            if re.fullmatch(r"[0\s\.,]+", c_clean):
                continue
            val = _turkish_number_to_decimal(c_clean)
            if val is None:
                continue
            # 3 haneli ve altını ele (altın fiyatı için anlamsız); istersen 1000 eşiğini değiştir
            if val < Decimal(1000):
                continue
            parsed.append(val)

        if parsed:
            best = max(parsed)  # penceredeki en makul büyük değer
            print(f"[INFO] Neighborhood (text) ile bulundu: {best}")
            return best

    # DEBUG: ilk 'Cumhuriyet' çevresini logla
    m_first = re.search(r"Cumhuriyet", html, flags=re.IGNORECASE)
    if m_first:
        i = m_first.start()
        lo = max(0, i - 200)
        hi = min(len(html), i + 600)
        context = BeautifulSoup(html[lo:hi], "html.parser").get_text(" ", strip=True)
        print("[DEBUG] 'Cumhuriyet' çevresi (metin):")
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

    # 0) Tablo başlık–sütun eşlemesiyle dene
    price = parse_price_via_table(html)

    # 1) BS4 (serbest metin) ile dene
    if price is None:
        price = parse_price_with_bs4(html)

    # 2) Regex fallback
    if price is None:
        price = parse_price_with_regex(html)

    # 3) Komşuluk (BS4 ile düz metne çevirip sayı seçme)
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
