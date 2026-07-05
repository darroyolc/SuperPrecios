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

BASE_URL = "https://www.aeg.com.es"
CHANNEL_PARAM = "cs02_corporatebenefits"
DOMAIN = "www.aeg.com.es"

# Únicas secciones por las que merece la pena rastrear (confirmado por log real)
INCLUDE_PATH_PREFIXES = ["/kitchen/", "/laundry/", "/aspiradoras/"]

# Exclusiones confirmadas por RUTA en dos rastreos reales distintos.
# Esta es la única fuente de exclusión — el texto de los enlaces no es fiable.
EXCLUDE_PATH_FRAGMENTS = ["/accessories/", "/kitchen/small-kitchen-appliances"]

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
    lower = path.lower()
    if any(frag in lower for frag in EXCLUDE_PATH_FRAGMENTS):
        return False
    if is_non_page(lower):
        return False
    return any(lower.startswith(prefix) for prefix in INCLUDE_PATH_PREFIXES)


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


def classify_price(text: str):
    """
    Heurística: si la página tiene entre 1 y 4 precios visibles, la tratamos
    como ficha de producto individual (el resto suele ser precio anterior,
    precio/mes de financiación, etc). Si hay más, es probable que sea un
    listado con varios productos y no intentamos sacar "el" precio de ahí.
    """
    matches = PRICE_PATTERN.findall(text)
    if not matches or len(matches) > 4:
        return None, False
    raw = matches[0].replace("€", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(raw), True
    except ValueError:
        return None, False


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

            try:
                links = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => ({href: e.href, text: e.innerText || ''}))"
                )
            except Exception:
                links = []

            try:
                body_text = page.inner_text("body")
            except Exception:
                body_text = ""

            price, is_product = classify_price(body_text)
            if is_product:
                try:
                    name = page.locator("h1").first.inner_text(timeout=2000).strip()
                except Exception:
                    name = page.title().strip()
                canon = canonical_url(raw_url)
                segments = [s for s in urlsplit(canon).path.strip("/").split("/") if s]
                sku = segments[-1] if segments else canon
                products[canon] = {"url": canon, "name": name, "sku": sku}
                print(f"[producto] {name} -> {price} € ({canon})")

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
    print(f"[info] Rutas excluidas por configuración: {', '.join(EXCLUDE_PATH_FRAGMENTS)}")
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
