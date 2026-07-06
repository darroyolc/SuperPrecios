"""
Descubre las URLs de producto en aeg.com.es (canal de beneficios corporativos).

Ajustado dos veces a partir de logs reales de ejecución:

1ª vuelta: el rastreo excluía por el TEXTO de cada enlace ("accesorio",
"pequeño electrodoméstico"). Un mega-menú hacía que el enlace de "Cocción"
incluyera en su innerText todo el panel desplegable, así que se excluían
categorías enteras sin relación.

2ª vuelta: se acotó el filtro de texto a textos cortos, pero SIGUIÓ
fallando — kitchen/cooking, laundry/laundry, etc. se excluían igual. La
causa real: hay enlaces cortos tipo "Accesorios" que apuntan a la MISMA
URL base de una categoría pero con un parámetro de filtro distinto
(?algo=accesorios). Como las URLs se comparan ignorando parámetros para
no rastrear la misma página dos veces, ese enlace corto "contaminaba" el
prefijo de la categoría entera.

Conclusión: el texto de los enlaces no es fiable en esta web. Esta versión
YA NO excluye nada por texto — solo por RUTA, usando las dos rutas
confirmadas en dos rastreos reales distintos:
    /accessories/                       -> "Accesorios"
    /kitchen/small-kitchen-appliances   -> "Pequeño electrodoméstico"

El texto de los enlaces solo se usa para registrar en el log qué se HABRÍA
excluido por texto (a título informativo), sin que afecte al rastreo.

Sigue usando Playwright porque el catálogo y los precios se cargan con
JavaScript, no están en el HTML inicial.

Guarda el resultado en data/products.json
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

from playwright.sync_api import sync_playwright

from price_utils import extract_product_price

BASE_URL = "https://www.aeg.com.es"
CHANNEL_PARAM = "cs02_corporatebenefits"
DOMAIN = "www.aeg.com.es"

# --- Qué se ignora del rastreo (enfoque de LISTA NEGRA) --------------------
# Antes el crawler solo entraba en 3 secciones (lista blanca), lo que dejaba
# fuera cualquier producto de otra zona. Ahora rastrea TODO el dominio y solo
# ignora lo que se lista aquí.

# Rutas de PRODUCTO que se ignoran (las que pediste). Se comparan como prefijo
# de ruta, así que cubren todo lo que cuelgue de ellas.
EXCLUDE_PATH_FRAGMENTS = [
    "/accessories/accessories",
    "/kitchen/small-kitchen-appliances",
    "/laundry/laundry/irons",
    "/laundry/laundry/garment-steamer",
]

# Secciones que NO son de producto (soporte, ayuda, blog, etc.). No contienen
# fichas; se saltan únicamente para que el rastreo semanal no se vaya de tiempo
# entrando en cientos de páginas irrelevantes. Confirmado: en aeg.com.es todos
# los productos cuelgan de /kitchen/, /laundry/ y /aspiradoras/, y lo no-producto
# vive bajo /support/ y el subdominio support.aeg.com.es (este último ni se sigue
# porque no es www.aeg.com.es). Si algún día hubiera productos en otra sección,
# basta con quitarla de esta lista.
NON_PRODUCT_PREFIXES = [
    "/support", "/about-aeg", "/about", "/faq", "/local",
    "/legal", "/promotions", "/store", "/newsletter", "/campaigns",
]

# Rutas que nunca son fichas de producto (assets, PDFs, avisos legales)
NON_PAGE_PATH_FRAGMENTS = ["/siteassets/", "/external/", "/overlays/"]
NON_PAGE_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg",
    ".zip", ".doc", ".docx", ".xls", ".xlsx", ".mp4",
)

# Solo informativo: registra enlaces con este texto para que puedas
# revisarlos tú, pero NO se usa para excluir nada del rastreo.
SUSPICIOUS_TEXT_PATTERN = re.compile(r"accesori|pequeñ.{0,10}electrodom", re.IGNORECASE)

# Patrón de precio en formato español: 1.234,56 € / 799 € / 69,90 €
PRICE_PATTERN = re.compile(r"\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s?€")

MAX_PAGES = 1000
REQUEST_DELAY = 0.4

COOKIE_BUTTON_TEXTS = ["aceptar todas", "aceptar cookies", "aceptar", "accept all", "accept"]


def with_channel(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    query["channel"] = [CHANNEL_PARAM]
    new_query = urlencode(query, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def dedup_key(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.netloc}{path}"


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme}://{parts.netloc}{path}/"


def is_non_page(path: str) -> bool:
    lower = path.lower()
    if any(frag in lower for frag in NON_PAGE_PATH_FRAGMENTS):
        return True
    return lower.rstrip("/").endswith(NON_PAGE_EXTENSIONS)


def is_in_scope(path: str) -> bool:
    """Lista negra: entra cualquier ruta del dominio salvo las rutas de
    producto excluidas, las secciones que no son de producto y los assets."""
    lower = path.lower()
    if any(lower.startswith(frag) for frag in EXCLUDE_PATH_FRAGMENTS):
        return False
    if any(lower == p or lower.startswith(p + "/") for p in NON_PRODUCT_PREFIXES):
        return False
    if is_non_page(lower):
        return False
    return True


def dismiss_cookie_banner(page):
    for text in COOKIE_BUTTON_TEXTS:
        try:
            btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE)).first
            if btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def autoscroll(page, rounds=6, pause=500):
    """Baja la página varias veces para forzar la carga de rejillas de producto
    que solo aparecen al hacer scroll (lazy-load). Para en cuanto la altura de
    la página deja de crecer, así no gasta tiempo de más."""
    try:
        last_height = 0
        for _ in range(rounds):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(pause)
            height = page.evaluate("document.body.scrollHeight")
            if height == last_height:
                break
            last_height = height
    except Exception:
        pass


# Fragmentos de URL que NO son fichas de producto reales aunque acaben en algo
# con pinta de código: redirecciones del CMS y páginas .aspx.
JUNK_URL_FRAGMENTS = ("~/link/", "/~/", ".aspx")


def looks_like_product_url(path: str) -> bool:
    """
    Una ficha de producto termina en un código de modelo (SKU) — un segmento
    que contiene al menos un "token" con letras Y números mezclados
    (lfr8504l6q, v5pba521ab, si6-1-2mn). Las páginas de listado terminan en
    palabras (ovens, built-in-dishwasher, 9-kg-washing-machine), donde números
    y palabras van en tokens SEPARADOS y por tanto no cuentan.

    Esto sustituye a la antigua heurística de "contar precios", que fallaba
    porque las fichas de producto reales muestran precio actual + precio
    anterior + financiación, superando el tope y quedando descartadas.
    """
    lower = path.lower()
    if any(frag in lower for frag in JUNK_URL_FRAGMENTS):
        return False
    segments = [s for s in path.strip("/").split("/") if s]
    if len(segments) < 2:
        return False
    last = segments[-1]
    for token in last.split("-"):
        has_letter = any(c.isalpha() for c in token)
        has_digit = any(c.isdigit() for c in token)
        if has_letter and has_digit:
            return True
    return False


def crawl():
    seen = set()
    to_visit = [BASE_URL + "/"]
    products = {}
    first_page = True
    pdf_skipped = 0
    suspicious_text_seen = {}  # link_key -> texto (solo informativo)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )

        while to_visit and len(seen) < MAX_PAGES:
            raw_url = to_visit.pop(0)
            key = dedup_key(raw_url)
            if key in seen:
                continue
            seen.add(key)

            path = urlsplit(raw_url).path
            is_homepage = path in ("", "/")

            if not is_homepage and not is_in_scope(path):
                continue

            try:
                page.goto(with_channel(raw_url), wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
            except Exception as e:
                print(f"[aviso] no se pudo cargar {raw_url}: {e}")
                continue

            if first_page:
                dismiss_cookie_banner(page)
                first_page = False

            # En páginas de listado/categoría, bajar para cargar toda la rejilla
            # de productos (lazy-load). En fichas de producto no hace falta.
            is_product_page = looks_like_product_url(path)
            if not is_product_page:
                autoscroll(page)

            try:
                links = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => ({href: e.href, text: e.innerText || ''}))"
                )
            except Exception:
                links = []

            if is_product_page:
                try:
                    name = page.locator("h1").first.inner_text(timeout=2000).strip()
                except Exception:
                    name = page.title().strip()
                price, method = extract_product_price(page)
                canon = canonical_url(raw_url)
                segments = [s for s in urlsplit(canon).path.strip("/").split("/") if s]
                sku = segments[-1] if segments else canon
                products[canon] = {"url": canon, "name": name, "sku": sku}
                price_txt = f"{price:.2f} € [{method}]" if price is not None else "precio no detectado"
                print(f"[producto] {name} -> {price_txt} ({canon})")

            for link in links:
                href = link.get("href", "")
                text = (link.get("text") or "").strip()
                if not href or DOMAIN not in href:
                    continue
                link_key = dedup_key(href)
                link_path = urlsplit(href).path

                if SUSPICIOUS_TEXT_PATTERN.search(text) and len(text) <= 80:
                    suspicious_text_seen.setdefault(link_key, text[:80])

                if is_non_page(link_path):
                    pdf_skipped += 1
                    continue
                if not is_in_scope(link_path):
                    continue
                if link_key not in seen:
                    to_visit.append(href)

            if len(seen) % 20 == 0:
                print(
                    f"[progreso] {len(seen)} páginas visitadas, "
                    f"{len(products)} productos encontrados, {len(to_visit)} en cola"
                )

            time.sleep(REQUEST_DELAY)

        browser.close()

    print(f"\n[info] Enlaces a PDF/assets descartados sin visitar: {pdf_skipped}")
    print(f"[info] Rutas de producto ignoradas: {', '.join(EXCLUDE_PATH_FRAGMENTS)}")
    print(f"[info] Secciones no-producto saltadas: {', '.join(NON_PRODUCT_PREFIXES)}")
    if suspicious_text_seen:
        print(
            "[info] Enlaces DENTRO del catálogo cuyo texto menciona "
            "'accesorio'/'pequeño electrodoméstico' (NO excluidos, solo aviso "
            "para que los revises tú si quieres):"
        )
        for link_key, text in sorted(suspicious_text_seen.items()):
            print(f"        - {text!r} -> {link_key}")

    return list(products.values())


def main():
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    products = crawl()
    out_path = data_dir / "products.json"
    out_path.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nGuardados {len(products)} productos en {out_path}")


if __name__ == "__main__":
    main()
