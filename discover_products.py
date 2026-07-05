"""
Descubre las URLs de producto en aeg.com.es (canal de beneficios corporativos),
excluyendo cualquier sección de navegación cuyo texto contenga "accesorio" o
"pequeño/s electrodoméstico/s".

IMPORTANTE — léelo antes de tocar nada:
La web carga el catálogo mediante JavaScript, así que no se puede "leer" con
una simple petición HTTP. Este script usa Playwright, que abre un navegador
real (headless) y espera a que la página se renderice.

Como no se puede inspeccionar el HTML final de la web desde fuera de un
navegador, la detección de "esto es una ficha de producto" se basa en una
heurística (busca un patrón de precio en euros en la página) en lugar de
clases CSS concretas. Es razonablemente robusta, pero si la primera
ejecución encuentra 0 productos o muy pocos, revisa los logs (el script
imprime el progreso) y ejecuta con DEBUG=1 para más detalle.

Guarda el resultado en data/products.json
"""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

from playwright.sync_api import sync_playwright

BASE_URL = "https://www.aeg.com.es"
CHANNEL_PARAM = "cs02_corporatebenefits"
DOMAIN = "www.aeg.com.es"

# Cualquier enlace de navegación cuyo TEXTO visible encaje con esto se
# considera una categoría a excluir (junto con todo lo que cuelgue de su URL).
EXCLUDE_TEXT_PATTERN = re.compile(r"accesori|pequeñ.{0,10}electrodom", re.IGNORECASE)

# Patrón de precio en formato español: 1.234,56 € / 799 € / 69,90 €
PRICE_PATTERN = re.compile(r"\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s?€")

MAX_PAGES = 800
REQUEST_DELAY = 0.4
DEBUG = os.environ.get("DEBUG") == "1"

COOKIE_BUTTON_TEXTS = ["aceptar todas", "aceptar cookies", "aceptar", "accept all", "accept"]


def with_channel(url: str) -> str:
    """Asegura que la URL lleva el parámetro de canal de beneficios corporativos."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    query["channel"] = [CHANNEL_PARAM]
    new_query = urlencode(query, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def dedup_key(url: str) -> str:
    """Clave usada para no visitar la misma página dos veces (ignora query/slash final)."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.netloc}{path}"


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme}://{parts.netloc}{path}/"


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
    excluded_prefixes = set()
    products = {}
    first_page = True

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

            if any(key.startswith(prefix) for prefix in excluded_prefixes):
                continue

            try:
                page.goto(with_channel(raw_url), wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1800)
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
                if DEBUG:
                    print(f"[producto] {name} -> {price} € ({canon})")

            for link in links:
                href = link.get("href", "")
                text = (link.get("text") or "").strip()
                if not href or DOMAIN not in href:
                    continue
                link_key = dedup_key(href)

                if EXCLUDE_TEXT_PATTERN.search(text):
                    excluded_prefixes.add(link_key)
                    continue
                if any(link_key.startswith(prefix) for prefix in excluded_prefixes):
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

    if excluded_prefixes:
        print("[info] Rutas excluidas por texto de navegación:")
        for prefix in sorted(excluded_prefixes):
            print(f"        - {prefix}")

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
