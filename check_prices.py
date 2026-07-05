"""
Revisa el precio actual de cada producto guardado en data/products.json y
avisa por Telegram si:

  1) el precio baja de PRICE_THRESHOLD euros, y/o
  2) el precio baja un DISCOUNT_THRESHOLD (50%) o más respecto a su "precio
     de referencia" — el precio original que la propia web muestre tachado
     junto al actual, o si no muestra ninguno, el precio más alto que el
     bot haya visto nunca para ese producto (se va actualizando solo).

Pensado para ejecutarse a menudo (cada pocas horas) vía GitHub Actions.
Usa Playwright porque el precio se carga con JavaScript en la web de AEG.

Guarda dos cosas en data/:
  - state.json      -> último precio, precio de referencia y si cada
                        condición ya estaba activa (para no repetir el
                        mismo aviso en cada ejecución)
  - alerts_log.json -> historial de TODOS los avisos que se han mandado,
                        para poder consultarlo más adelante
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

import requests
from playwright.sync_api import sync_playwright

PRICE_THRESHOLD = 100.0
DISCOUNT_THRESHOLD = 0.50  # 50%
CHANNEL_PARAM = "cs02_corporatebenefits"
PRICE_PATTERN = re.compile(r"\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s?€")
REQUEST_DELAY = 0.3

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

DATA_DIR = Path(__file__).parent / "data"
PRODUCTS_FILE = DATA_DIR / "products.json"
STATE_FILE = DATA_DIR / "state.json"
ALERTS_LOG_FILE = DATA_DIR / "alerts_log.json"


def with_channel(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    query["channel"] = [CHANNEL_PARAM]
    new_query = urlencode(query, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def parse_price(raw: str) -> float:
    """Convierte '1.234,56 €' / '799 €' / '69,90 €' (formato español) a float."""
    cleaned = raw.replace("€", "").strip().replace(".", "").replace(",", ".")
    return float(cleaned)


def find_original_price(page):
    """
    Busca un precio mostrado tachado (CSS text-decoration: line-through),
    que suele ser como las webs marcan el precio "antes de descuento".
    Funciona sin importar si usan <del>, <s> o una clase CSS propia.
    """
    try:
        texts = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('body *'))
                .filter(el => el.children.length === 0)
                .filter(el => getComputedStyle(el).textDecorationLine.includes('line-through'))
                .map(el => el.innerText)
                .filter(Boolean)
            """
        )
    except Exception:
        return None
    for t in texts:
        m = PRICE_PATTERN.search(t)
        if m:
            try:
                return parse_price(m.group(0))
            except ValueError:
                continue
    return None


def extract_prices(page):
    """Devuelve (precio_actual, precio_original_o_None)."""
    original = find_original_price(page)
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    current = None
    for m in PRICE_PATTERN.findall(text):
        try:
            val = parse_price(m)
        except ValueError:
            continue
        if original is not None and abs(val - original) < 0.01:
            continue  # es el mismo precio tachado, no el actual
        current = val
        break
    return current, original


def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[aviso] Faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID; no se envía nada.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"[error] Telegram respondió {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"[error] no se pudo avisar por Telegram: {e}")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def main():
    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})
    alerts_log = load_json(ALERTS_LOG_FILE, [])

    if not products:
        print("data/products.json está vacío. Ejecuta antes discover_products.py")
        return

    alerts_sent = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )

        for product in products:
            url = product["url"]
            sku = product.get("sku", url)
            name = product.get("name", url)

            try:
                page.goto(with_channel(url), wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
            except Exception as e:
                print(f"[aviso] no se pudo comprobar {url}: {e}")
                continue

            current_price, page_original_price = extract_prices(page)
            if current_price is None:
                print(f"[aviso] no se encontró precio en {url}")
                time.sleep(REQUEST_DELAY)
                continue

            prev = state.get(sku, {})

            # El precio de referencia nunca baja: es el máximo entre lo que
            # ya sabíamos, lo que la web muestra tachado (si lo muestra) y
            # el propio precio actual.
            reference_price = max(
                prev.get("reference_price", 0.0),
                page_original_price or 0.0,
                current_price,
            )

            discount_pct = 0.0
            if reference_price > 0:
                discount_pct = (reference_price - current_price) / reference_price

            under_100 = current_price < PRICE_THRESHOLD
            big_discount = reference_price > current_price and discount_pct >= DISCOUNT_THRESHOLD

            was_under_100 = prev.get("was_under_100", False)
            was_big_discount = prev.get("was_big_discount", False)

            reasons = []
            if under_100 and not was_under_100:
                reasons.append(f"por debajo de {PRICE_THRESHOLD:.0f} €")
            if big_discount and not was_big_discount:
                reasons.append(
                    f"ha bajado un {discount_pct * 100:.0f}% respecto a su precio de "
                    f"referencia ({reference_price:.2f} €)"
                )

            if reasons:
                message = (
                    f"🔔 <b>{name}</b>\n"
                    f"Motivo: {' y '.join(reasons)}\n"
                    f"Precio actual: <b>{current_price:.2f} €</b>\n"
                    f"{url}"
                )
                send_telegram(message)
                alerts_sent += 1
                print(f"[alerta] {name}: {current_price:.2f} € ({', '.join(reasons)})")

                alerts_log.append(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "sku": sku,
                        "name": name,
                        "url": url,
                        "price": current_price,
                        "reference_price": reference_price,
                        "discount_pct": round(discount_pct, 4),
                        "reasons": reasons,
                    }
                )
            else:
                print(f"[ok] {name}: {current_price:.2f} € (referencia: {reference_price:.2f} €)")

            state[sku] = {
                "name": name,
                "last_price": current_price,
                "reference_price": reference_price,
                "was_under_100": under_100,
                "was_big_discount": big_discount,
            }
            time.sleep(REQUEST_DELAY)

        browser.close()

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    ALERTS_LOG_FILE.write_text(
        json.dumps(alerts_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nRevisión completa. Avisos enviados: {alerts_sent}")


if __name__ == "__main__":
    main()
