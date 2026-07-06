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


# Pistas de financiación. Las de SUFIJO van justo DESPUÉS de la cifra
# ("24,90 €/mes", "€ al mes"); la de PREFIJO ("12 cuotas de 41,58 €") va antes
# del importe. El "0€ de entrada" no necesita pista: se descarta por ser 0.
FINANCING_SUFFIX = ("/mes", "al mes", "mensual")
FINANCING_PREFIX = ("cuota",)


def extract_selling_price(text: str):
    """
    Devuelve el primer precio "real" de la página (float) o None.
    Ignora cifras de financiación (0€ de entrada, X€/mes, cuotas...) y ceros.

    Para no confundir el precio real con la letra pequeña de financiación, a
    cada cifra se le mira una ventana CORTA por detrás (por el sufijo "/mes")
    y el hueco desde la cifra anterior (por el prefijo "cuota"). Así "/mes"
    no se contamina con el precio siguiente y "cuota" no salta al total real.
    """
    prev_end = 0
    for m in PRICE_PATTERN.finditer(text):
        after = text[m.end():m.end() + 8].lower()
        between = text[prev_end:m.start()].lower()
        prev_end = m.end()
        if any(h in after for h in FINANCING_SUFFIX):
            continue
        if any(h in between for h in FINANCING_PREFIX):
            continue
        raw = m.group(0).replace("€", "").strip().replace(".", "").replace(",", ".")
        try:
            val = float(raw)
        except ValueError:
            continue
        if val <= 0:
            continue
        return val
    return None


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

            if looks_like_product_url(path):
                try:
                    name = page.locator("h1").first.inner_text(timeout=2000).strip()
                except Exception:
                    name = page.title().strip()
                price = extract_selling_price(body_text)
                canon = canonical_url(raw_url)
                segments = [s for s in urlsplit(canon).path.strip("/").split("/") if s]
                sku = segments[-1] if segments else canon
                products[canon] = {"url": canon, "name": name, "sku": sku}
                price_txt = f"{price:.2f} €" if price is not None else "precio no detectado"
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
