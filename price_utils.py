"""
Extracción robusta del precio de producto en las fichas de AEG.

Por qué existe este módulo: leer "la primera cifra en € de la página" no
funciona. Las fichas de AEG muestran, además del precio, bloques de
servicio/garantía ("Plan de protección 69,90 €"), financiación
("0€ de entrada", "24,90 €/mes") y accesorios, y la cifra equivocada se
colaba como precio (por eso salían 12 productos distintos todos a 69,90 €).

Estrategia, de más fiable a menos:
  1) JSON-LD (schema.org/Product) — el dato estructurado que las webs de
     e-commerce incrustan con el precio canónico del producto.
  2) Etiquetas <meta> de precio (itemprop / OpenGraph / product:price).
  3) Como último recurso, el texto de la página, pero RECORTANDO desde la
     primera sección de servicio/garantía/accesorios para no coger su precio,
     e ignorando la letra pequeña de financiación.

`extract_product_price(page)` devuelve (precio_float_o_None, método_usado),
donde método es 'json-ld' | 'meta' | 'body' | '-'. El método se registra en
los logs para poder diagnosticar de dónde salió cada precio.
"""
import json
import re

PRICE_PATTERN = re.compile(r"\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s?€")

# Letra pequeña de financiación: sufijo tras la cifra ("24,90 €/mes"),
# prefijo antes del importe ("12 cuotas de 41,58 €"). El "0€ de entrada"
# se descarta por ser 0.
FINANCING_SUFFIX = ("/mes", "al mes", "mensual")
FINANCING_PREFIX = ("cuota",)

# Al leer el texto como último recurso, se corta desde la primera de estas
# secciones para no coger el precio de un servicio, garantía o accesorio.
SERVICE_SECTION_MARKERS = (
    "servicio técnico", "servicios aeg", "plan de protección",
    "amplía tu garantía", "amplía la garantía", "extiende tu garantía",
    "garantía adicional", "protección y mantenimiento", "seguro de",
    "productos relacionados", "también te puede interesar",
    "te puede interesar", "accesorios", "recambios", "repuestos",
    "newsletter", "suscríbete", "opiniones", "valoraciones", "reseñas",
    "preguntas frecuentes", "registra tu producto",
)


def _num(x):
    """Convierte a float un precio venga como número o como cadena en
    formato español (1.234,56) o inglés (1,234.56)."""
    if isinstance(x, (int, float)):
        return float(x)
    if not isinstance(x, str):
        return None
    x = x.strip()
    if not x:
        return None
    if "," in x and "." in x:
        if x.rfind(",") > x.rfind("."):       # 1.234,56 -> es
            x = x.replace(".", "").replace(",", ".")
        else:                                  # 1,234.56 -> en
            x = x.replace(",", "")
    elif "," in x:
        x = x.replace(",", ".")
    try:
        return float(x)
    except ValueError:
        return None


def _iter_nodes(data):
    """Recorre recursivamente dicts y listas de un JSON (incluye @graph)."""
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _iter_nodes(v)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_nodes(item)


def _offers_price(offers):
    for node in _iter_nodes(offers):
        if isinstance(node, dict):
            for key in ("price", "lowPrice", "highPrice"):
                if key in node:
                    val = _num(node[key])
                    if val and val > 0:
                        return val
    return None


def price_from_jsonld(page):
    try:
        raws = page.eval_on_selector_all(
            'script[type="application/ld+json"]',
            "els => els.map(e => e.textContent)",
        )
    except Exception:
        return None
    for raw in raws or []:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for node in _iter_nodes(data):
            if not isinstance(node, dict):
                continue
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" in types and node.get("offers") is not None:
                price = _offers_price(node["offers"])
                if price:
                    return price
    return None


def price_from_meta(page):
    selectors = (
        'meta[itemprop="price"]',
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
    )
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            el = None
        if el:
            val = _num(el.get_attribute("content"))
            if val and val > 0:
                return val
    return None


def scan_text_price(text, exclude=None):
    """Primer precio "real" en un texto, ignorando financiación y, si se
    indica, un valor a excluir (p. ej. el precio tachado)."""
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
        if exclude is not None and abs(val - exclude) < 0.01:
            continue
        return val
    return None


def price_from_body(page, exclude=None):
    try:
        text = page.inner_text("body")
    except Exception:
        return None
    low = text.lower()
    cut = len(text)
    for marker in SERVICE_SECTION_MARKERS:
        i = low.find(marker)
        if 0 <= i < cut:
            cut = i
    return scan_text_price(text[:cut], exclude=exclude)


def extract_product_price(page, exclude=None):
    """Devuelve (precio_o_None, método). método: json-ld | meta | body | -"""
    val = price_from_jsonld(page)
    if val is not None:
        return val, "json-ld"
    val = price_from_meta(page)
    if val is not None:
        return val, "meta"
    val = price_from_body(page, exclude=exclude)
    if val is not None:
        return val, "body"
    return None, "-"
