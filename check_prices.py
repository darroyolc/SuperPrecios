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

from price_utils import PRICE_PATTERN, extract_product_price, is_excluded_product, stock_status

# Umbral de precio por defecto: avisa si un producto baja de esta cifra.
DEFAULT_PRICE_THRESHOLD = 100.0

# Umbrales por categoría (por prefijo de ruta). El primero que encaje manda; si
# ninguno encaja, se usa DEFAULT_PRICE_THRESHOLD. Vacío ahora mismo: las
# aspiradoras, que antes iban a 50 €, están excluidas del todo (ver
# EXCLUDED_PRODUCT_PATHS en price_utils). Añade aquí ("/ruta", importe) si
# quieres un umbral distinto para otra categoría.
CATEGORY_PRICE_THRESHOLDS = []

DISCOUNT_THRESHOLD = 0.50  # 50%
CHANNEL_PARAM = "cs02_corporatebenefits"
REQUEST_DELAY = 0.2

# Presupuesto de tiempo interno (el workflow mata el job a los 30 min). Al
# llegar aquí se corta el bucle, se guarda el estado y los avisos, y se sale
# limpio. Los productos que queden sin revisar se revisan en la siguiente
# pasada (cada 6 h); el estado se conserva entre ejecuciones.
MAX_RUNTIME_SECONDS = 26 * 60

# Productos a vigilar por STOCK (además del precio): avisa cuando uno pase de
# agotado a disponible. En aeg.com.es "hay precio/botón de compra" ≈ "hay
# stock". Se vigilan aunque estén agotados y sin precio (no se saltan como el
# resto de "sin precio"). Añade más URLs aquí si quieres vigilar su stock.
STOCK_WATCH_URLS = [
    "https://www.aeg.com.es/kitchen/cooking/cooker-hoods/integrated-hood/gde686hm2/",
]


def _url_key(url: str) -> str:
    """Clave de URL sin query ni barra final, para comparar robustamente."""
    return urlsplit(url).path.rstrip("/").lower()


_STOCK_WATCH_KEYS = {_url_key(u) for u in STOCK_WATCH_URLS}


def is_stock_watched(url: str) -> bool:
    return _url_key(url) in _STOCK_WATCH_KEYS

# Recursos que se bloquean para cargar más rápido. OJO: aquí NO se bloquea
# "stylesheet" (CSS) porque la detección del precio tachado usa getComputedStyle
# y necesita los estilos cargados. Sí se bloquean imágenes, vídeo y fuentes.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def block_heavy_resources(page):
    def _route(route):
        try:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass
    try:
        page.route("**/*", _route)
    except Exception:
        pass


def price_threshold_for(url: str) -> float:
    """Umbral de precio que aplica a este producto según su ruta."""
    path = urlsplit(url).path.lower()
    for prefix, threshold in CATEGORY_PRICE_THRESHOLDS:
        if path.startswith(prefix):
            return threshold
    return DEFAULT_PRICE_THRESHOLD

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
    """Devuelve (precio_actual, precio_original_o_None).

    El precio actual sale del extractor robusto (JSON-LD -> meta -> texto
    recortado), que evita coger precios de servicio/garantía/financiación.
    El precio original (tachado) se detecta aparte por el estilo CSS.
    """
    original = find_original_price(page)
    current, _method = extract_product_price(page, exclude=original)
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

    # Garantiza que las URLs vigiladas por stock se revisen aunque el
    # descubrimiento no las haya incluido (p. ej. por estar agotadas).
    known = {_url_key(p.get("url", "")) for p in products}
    for watch_url in STOCK_WATCH_URLS:
        if _url_key(watch_url) not in known:
            sku = [s for s in urlsplit(watch_url).path.strip("/").split("/") if s][-1]
            products.append({"url": watch_url, "name": watch_url, "sku": sku})

    alerts_sent = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        block_heavy_resources(page)

        start = time.monotonic()
        checked = 0
        for product in products:
            if time.monotonic() - start > MAX_RUNTIME_SECONDS:
                print(
                    f"[info] Límite de tiempo ({MAX_RUNTIME_SECONDS // 60} min) alcanzado; "
                    f"revisados {checked}/{len(products)}. Guardo estado y salgo; "
                    f"el resto se revisa en la próxima pasada."
                )
                break
            checked += 1

            url = product["url"]
            sku = product.get("sku", url)
            name = product.get("name", url)

            # Categorías excluidas (aspiradoras, planchas, centros de planchado,
            # accesorios, pequeño electrodoméstico): no se revisan aunque sigan
            # en products.json de un rastreo anterior.
            if is_excluded_product(url):
                continue

            try:
                page.goto(with_channel(url), wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
            except Exception as e:
                print(f"[aviso] no se pudo comprobar {url}: {e}")
                continue

            current_price, page_original_price = extract_prices(page)

            # Reintento si no salió precio: muchas fichas cargan el precio por
            # JSON-LD un poco tarde. Se recarga con una espera más larga y fija
            # (nada de networkidle, que puede colgarse) y se reintenta una vez.
            if current_price is None:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                try:
                    current_price, page_original_price = extract_prices(page)
                except Exception:
                    pass

            # --- Vigilancia de STOCK (solo URLs en STOCK_WATCH_URLS) ---
            # Va antes del salto por "sin precio": una ficha agotada no tiene
            # precio, pero igualmente queremos saber cuándo vuelve a haberlo.
            if is_stock_watched(url):
                status = stock_status(page)  # 'in' / 'out' / None(ilegible)
                if status is not None:
                    was_in = state.get(sku, {}).get("stock_in")  # True/False/None
                    is_in = status == "in"
                    if is_in and was_in is False:
                        send_telegram(
                            f"🟢 <b>¡VUELVE A HABER STOCK!</b>\n"
                            f"<b>{name}</b>\n{url}"
                        )
                        alerts_sent += 1
                        print(f"[stock] {name}: VUELVE A HABER STOCK")
                        alerts_log.append({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "sku": sku, "name": name, "url": url,
                            "reasons": ["vuelta de stock"],
                        })
                    else:
                        print(f"[stock] {name}: {'disponible' if is_in else 'agotado'}")
                    stock_entry = state.setdefault(sku, {})
                    stock_entry["name"] = name
                    stock_entry["stock_in"] = is_in

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

            threshold = price_threshold_for(url)
            under_threshold = current_price < threshold
            big_discount = reference_price > current_price and discount_pct >= DISCOUNT_THRESHOLD

            # Retrocompatibilidad: el estado antiguo usaba la clave "was_under_100".
            was_under_threshold = prev.get(
                "was_under_threshold", prev.get("was_under_100", False)
            )
            was_big_discount = prev.get("was_big_discount", False)

            reasons = []
            if under_threshold and not was_under_threshold:
                reasons.append(f"por debajo de {threshold:.0f} €")
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

            # update (no sobrescribir) para no borrar las claves de stock_in
            # que haya podido escribir el bloque de vigilancia de stock.
            state.setdefault(sku, {}).update({
                "name": name,
                "last_price": current_price,
                "reference_price": reference_price,
                "was_under_threshold": under_threshold,
                "was_big_discount": big_discount,
            })
            time.sleep(REQUEST_DELAY)

        browser.close()

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    ALERTS_LOG_FILE.write_text(
        json.dumps(alerts_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nRevisión completa. Avisos enviados: {alerts_sent}")


if __name__ == "__main__":
    main()
