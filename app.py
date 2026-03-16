# -*- coding: utf-8 -*-
import base64
import csv
import html
import io
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    import anthropic
except Exception:
    anthropic = None

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"}
TIMEOUT = 20
MAX_LISTING_PRODUCTS = 60
MAX_IMAGES = 15
IMAGE_BLOCKLIST = ("logo", "icon", "banner", "sprite", "placeholder")

CREATE_SYSTEM_PROMPT = (
    "Jestes ekspertem SEO dla polskiego e-commerce, specjalizujesz sie w pojazdach elektrycznych. "
    "Zwracasz TYLKO JSON: {name, short_description (max 280 zn bez HTML), description (HTML 400-700 slow: "
    "intro, h2 cechy, h2 zastosowanie, h2 dlaczego warto, CTA), seo_title (50-60 zn), "
    "seo_description (140-160 zn z CTA), seo_url (slug max 80)}. "
    "WAZNE: TYLKO JSON. Pisz po polsku z polskimi znakami w tresci."
)

REWRITE_SYSTEM_PROMPT = (
    "Jestes ekspertem SEO dla polskiego e-commerce. Przepisujesz istniejace opisy. "
    "Zwracasz TYLKO JSON: {name, short_description, description, seo_title, seo_description, seo_url}. "
    "ZASADY: zachowaj dane techniczne, przepisz wlasnym jezykiem, 400-700 slow. "
    "WAZNE: TYLKO JSON. Pisz po polsku z polskimi znakami w tresci."
)

MODEL_OPTIONS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
]

RAW_CATEGORIES = [
    "Pojazdy elektryczne > Dla dzieci i młodzieży",
    "Skutery elektryczne > Skuter 50 cm³ (L1e)",
    "Skutery elektryczne > Skuter 125 cm³ (L3e)",
    "Pojazdy elektryczne > Motocykle 50 cc",
    "Motocykle enduro elektryczne > L1e / do 50 cm³",
    "Pojazdy elektryczne > Motocykle 125 cc",
    "Pojazdy elektryczne > Microcar",
    "Pojazdy elektryczne > Quady",
    "Części i akcesoria > Pielęgnacja Motocykla",
    "Części i akcesoria > Personalizacja > Okleina Surron Ultra Bee",
    "Części i akcesoria > Personalizacja > Okleina Surron Light Bee",
    "Części i akcesoria > Personalizacja > Okleina Talaria MX3/MX4",
    "Części i akcesoria > Personalizacja > Projekt customowy",
    "Części i akcesoria > Personalizacja > Siedzenia",
    "Części i akcesoria > Personalizacja > Koła",
    "Pojazdy elektryczne > Skutery 125 cc",
    "Odzież > Kombinezony",
    "Modyfikacje > Akumulatory",
    "Modyfikacje > Surron Light Bee",
    "Pojazdy elektryczne > Hulajnogi",
    "Pojazdy elektryczne > Microcary",
    "Pojazdy elektryczne > Skutery 50 cc",
    "Pojazdy elektryczne > Delivery i Cargo",
    "Pojazdy elektryczne > Inwalidzkie",
    "Pojazdy elektryczne > Golfowe",
    "Motocykle enduro elektryczne > Off-road / bez homologacji (full cross)",
    "Części zamienne",
    "Motocykle enduro elektryczne > L3e / do 125 cm³",
    "Pojazdy elektryczne > Cross / Enduro",
    "Motocykle enduro elektryczne > Mini cross elektryczny dla dzieci i młodzieży",
    "Części i akcesoria",
    "Części zamienne > Części zamienne eRide Pro > Baterie i ładowarki",
    "Części zamienne > Części zamienne eRide Pro > Elektronika i sterowanie",
    "Akcesoria > Gogle motocyklowe > Gogle crossowe",
    "Części zamienne > Części zamienne eRide Pro > Silnik i układ napędowy",
    "Modyfikacje > Akcesoria TORP",
    "Modyfikacje > Silniki TORP",
    "Części zamienne > Części zamienne eRide Pro > Kierownica i sterowanie",
    "Wyjazdy",
]


# ==============================
# General / Normalization helpers
# ==============================
def safe_str(text):
    return str(text or "").encode("utf-8", "replace").decode("utf-8")


def normalize_whitespace(text):
    return re.sub(r"\s+", " ", safe_str(text)).strip()


def ascii_fold(text):
    text = normalize_whitespace(text)
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def slugify(text):
    text = ascii_fold(text).lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:80]


def parse_float(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = safe_str(value)
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    text = normalize_whitespace(text)
    text = re.sub(r"[^0-9,\.\-\s]", "", text)
    text = re.sub(r"(?<=\d)\s+(?=\d{3}(\D|$))", "", text)
    text = text.strip()

    match = re.search(r"(-?\d+(?:[.,]\d+)?)", text)
    if not match:
        return None

    number = match.group(1)
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    else:
        number = number.replace(",", ".")

    try:
        return float(number)
    except Exception:
        return None


def parse_urls(text):
    seen = set()
    result = []
    for line in safe_str(text).splitlines():
        url = line.strip()
        if not url or not url.startswith(("http://", "https://")):
            continue
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


SPEC_FIELD_ORDER = [
    "Waga",
    "Wymiary",
    "Homologacja",
    "Rodzaj",
    "Bateria",
    "Pojemność Baterii",
    "Czas ładowania Baterii",
    "Bateria wyciągana",
    "Napęd",
    "Prędkość Max",
    "Maksymalny Zasięg *",
    "Hamulce (Przód/Tył)",
    "Rozmiar Opon (Przód/Tył)",
    "Masa własna",
    "Dopuszczalna ładowność",
    "Dopuszczalna masa całkowita",
    "Wyświetlacz",
    "Kolor",
    "Wymagane uprawnienia",
]

# Ordered from most-specific to generic to avoid wrong matches.
SPEC_FIELD_ALIAS_RULES = [
    ("Dopuszczalna masa całkowita", ["dopuszczalna masa calkowita", "dmc", "masa calkowita", "gross vehicle mass", "gross weight", "gvwr"]),
    ("Dopuszczalna ładowność", ["dopuszczalna ladownosc", "ladownosc", "ładowność", "payload", "max load"]),
    ("Masa własna", ["masa wlasna", "masa własna", "masa pojazdu", "curb weight", "net weight"]),
    ("Rozmiar Opon (Przód/Tył)", ["rozmiar opon przod tyl", "rozmiar opon", "opony przod tyl", "rozmiar kol", "wheel size", "tire size", "tyre size"]),
    ("Hamulce (Przód/Tył)", ["hamulce przod tyl", "hamulce", "brake", "brakes"]),
    ("Czas ładowania Baterii", ["czas ladowania baterii", "czas ladowania", "czas ładowania baterii", "czas ładowania", "charging time", "charge time"]),
    ("Pojemność Baterii", ["pojemnosc baterii", "pojemność baterii", "battery capacity", "capacity"]),
    ("Bateria wyciągana", ["bateria wyciagana", "bateria wyjmowana", "removable battery", "swappable battery"]),
    ("Maksymalny Zasięg *", ["maksymalny zasieg", "zasięg maksymalny", "zasieg", "zasięg", "range", "max range"]),
    ("Prędkość Max", ["predkosc max", "prędkość max", "predkosc maksymalna", "prędkość maksymalna", "vmax", "max speed"]),
    ("Wymagane uprawnienia", ["wymagane uprawnienia", "uprawnienia", "prawo jazdy", "licencja", "license"]),
    ("Wyświetlacz", ["wyswietlacz", "wyświetlacz", "display"]),
    ("Napęd", ["naped", "napęd", "drive", "motor power", "moc silnika"]),
    ("Homologacja", ["homologacja", "homologation", "dopuszczenie do ruchu"]),
    ("Rodzaj", ["rodzaj", "kategoria", "typ pojazdu", "type"]),
    ("Bateria", ["bateria", "battery", "typ baterii"]),
    ("Wymiary", ["wymiary", "dimensions", "dimension"]),
    ("Kolor", ["kolor", "kolorystyka", "color"]),
    ("Waga", ["waga", "weight"]),
]

SPEC_BLOCK_START = "<!-- gc-spec-start -->"
SPEC_BLOCK_END = "<!-- gc-spec-end -->"


def normalize_spec_label_key(label):
    key = ascii_fold(label).lower()
    key = key.replace("×", "x")
    key = re.sub(r"[^a-z0-9]+", " ", key)
    return normalize_whitespace(key)


def normalize_spec_value(value):
    text = normalize_whitespace(value)
    if not text:
        return ""
    text = re.sub(r"\b(wybierz opcje|wybierz opcję|choose an option|wyczysc|wyczyść|clear)\b", " ", text, flags=re.IGNORECASE)
    text = normalize_whitespace(text.strip(" ,;|"))
    if not text:
        return ""
    if normalize_spec_label_key(text) in {"", "-", "brak", "n a", "na", "nie dotyczy", "specyfikacja", "specification", "parametry", "dane techniczne"}:
        return ""
    return text


def map_spec_label_to_target(raw_label):
    key = normalize_spec_label_key(raw_label)
    if not key:
        return ""
    for target, aliases in SPEC_FIELD_ALIAS_RULES:
        alias_keys = [normalize_spec_label_key(target)] + [normalize_spec_label_key(alias) for alias in aliases]
        for alias_key in alias_keys:
            if not alias_key:
                continue
            if key == alias_key:
                return target
            # Only allow prefix match for multi-word aliases (e.g. "predkosc max ..."),
            # so generic one-word labels like "waga" do not match "maksymalna waga uzytkownika".
            if " " in alias_key and key.startswith(f"{alias_key} "):
                return target
    return ""


def spec_value_quality(value):
    text = normalize_spec_value(value)
    if not text:
        return -1
    score = len(text)
    low = ascii_fold(text).lower()
    if re.search(r"\d", text):
        score += 20
    if any(unit in low for unit in ["kg", "km", "km/h", "v", "ah", "wh", "w", "nm", "cm", "mm", "godz", "h"]):
        score += 12
    if any(token in text for token in ["/", "×", "x"]):
        score += 4
    if len(text) > 180:
        score -= 8
    return score


def upsert_spec_field(spec_fields, raw_label, raw_value):
    target = map_spec_label_to_target(raw_label)
    if not target:
        return
    value = normalize_spec_value(raw_value)
    if not value:
        return
    existing = normalize_spec_value(spec_fields.get(target, ""))
    if not existing:
        spec_fields[target] = value
        return
    if normalize_spec_label_key(existing) == normalize_spec_label_key(value):
        return
    if spec_value_quality(value) > spec_value_quality(existing):
        spec_fields[target] = value


def normalize_spec_fields(spec_fields):
    normalized = {}
    if not isinstance(spec_fields, dict):
        return normalized
    for field in SPEC_FIELD_ORDER:
        value = normalize_spec_value(spec_fields.get(field, ""))
        if value:
            normalized[field] = value
    return normalized


def extract_spec_value_from_cell(cell):
    option_values = []
    seen = set()
    for option in cell.select("option"):
        raw = normalize_spec_value(option.get_text(" ", strip=True))
        if not raw:
            continue
        folded = normalize_spec_label_key(raw)
        if folded in {"", "wybierz opcje", "wybierz opcję", "choose an option", "wyczysc", "wyczyść", "clear"}:
            continue
        if folded not in seen:
            seen.add(folded)
            option_values.append(raw)
    if option_values:
        return ", ".join(option_values)
    return normalize_spec_value(cell.get_text(" ", strip=True))


def extract_spec_fields(soup, page_text, json_ld_items, weight=None):
    spec_fields = {}
    main_product = (
        soup.select_one("main .product.type-product")
        or soup.select_one(".single-product div.product")
        or soup.select_one("div.product.type-product")
        or soup.select_one("main div.product")
        or soup.body
    )

    # Tables (WooCommerce attributes, variation tables and custom technical tables).
    for table in main_product.select("table"):
        if is_blocked_text_node(table):
            continue
        for row in table.select("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            if row.find("th") and row.find("td"):
                label = normalize_whitespace(row.find("th").get_text(" ", strip=True))
                value_cell = row.find_all("td")[-1]
            else:
                label = normalize_whitespace(cells[0].get_text(" ", strip=True))
                value_cell = cells[-1]
            value = extract_spec_value_from_cell(value_cell)
            upsert_spec_field(spec_fields, label, value)

    # Definition lists used by some themes.
    for dl in main_product.select("dl"):
        if is_blocked_text_node(dl):
            continue
        terms = dl.find_all("dt")
        for dt in terms:
            dd = dt.find_next_sibling("dd")
            if dd is None:
                continue
            upsert_spec_field(
                spec_fields,
                dt.get_text(" ", strip=True),
                extract_spec_value_from_cell(dd),
            )

    # Fallback for inline "Label: value" text.
    inline_sources = main_product.select(".summary, .entry-summary, .woocommerce-Tabs-panel, .woocommerce-product-details__short-description, .entry-content, p, li")
    for node in inline_sources:
        if is_blocked_text_node(node):
            continue
        text = normalize_whitespace(node.get_text(" ", strip=True))
        if not text or ":" not in text:
            continue
        match = re.match(r"^\s*([^:]{2,70})\s*:\s*(.+?)\s*$", text)
        if not match:
            continue
        label = normalize_whitespace(match.group(1))
        value = normalize_whitespace(match.group(2))
        upsert_spec_field(spec_fields, label, value)

    # Ensure core fields from dedicated extractors are not lost.
    weight_value = parse_float(weight)
    if weight_value is not None and not spec_fields.get("Waga"):
        if weight_value.is_integer():
            spec_fields["Waga"] = f"{int(weight_value)} kg"
        else:
            spec_fields["Waga"] = f"{weight_value:g} kg"

    # JSON-LD fallback for dimensions when available.
    if not spec_fields.get("Wymiary"):
        for data in json_ld_items:
            for item in flatten_json_ld(data):
                if not isinstance(item, dict):
                    continue
                width = normalize_spec_value(item.get("width"))
                height = normalize_spec_value(item.get("height"))
                depth = normalize_spec_value(item.get("depth") or item.get("length"))
                dims = [x for x in [width, depth, height] if x]
                if len(dims) >= 2:
                    spec_fields["Wymiary"] = " × ".join(dims)
                    break
            if spec_fields.get("Wymiary"):
                break

    return normalize_spec_fields(spec_fields)


def spec_fields_as_text(spec_fields):
    normalized = normalize_spec_fields(spec_fields)
    if not normalized:
        return ""
    return "\n".join([f"{field}: {normalized[field]}" for field in SPEC_FIELD_ORDER if normalized.get(field)])


def render_specification_block(spec_fields):
    normalized = normalize_spec_fields(spec_fields)
    if not normalized:
        return ""
    rows = []
    for field in SPEC_FIELD_ORDER:
        value = normalized.get(field)
        if not value:
            continue
        rows.append(
            "<div style=\"padding:10px 0;border-top:1px solid #e2e8f0;\">"
            f"<div style=\"font-weight:700;\">{html.escape(field)}</div>"
            f"<div style=\"margin-top:4px;\">{html.escape(value)}</div>"
            "</div>"
        )
    if not rows:
        return ""
    return (
        f"{SPEC_BLOCK_START}\n"
        "<div class=\"gc-spec-block\" style=\"margin-top:20px;padding:16px;border:1px solid #e2e8f0;border-radius:12px;background:#f8fafc;color:#0f172a;\">"
        "<h3 style=\"margin:0 0 8px 0;\">Specyfikacja techniczna</h3>"
        + "".join(rows) +
        "</div>\n"
        f"{SPEC_BLOCK_END}"
    )


def description_has_external_spec_section(description_html):
    blob = ascii_fold(description_html).lower()
    if "gc-spec-start" in blob:
        return True
    if "specyfikac" not in blob and "<table" not in blob:
        return False
    checks = [
        "homologacja",
        "pojemnosc baterii",
        "predkosc max",
        "maksymalny zasieg",
        "naped",
        "wymiary",
        "waga",
    ]
    hits = sum(1 for token in checks if token in blob)
    return hits >= 3


def strip_generated_spec_block(description_html):
    text = safe_str(description_html)
    text = re.sub(
        r"<!--\s*gc-spec-start\s*-->.*?<!--\s*gc-spec-end\s*-->",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text.strip()


def attach_specification_block(description_html, spec_fields):
    base_description = strip_generated_spec_block(description_html)
    spec_block = render_specification_block(spec_fields)
    if not spec_block:
        return base_description
    if description_has_external_spec_section(base_description):
        return base_description
    if not base_description:
        return spec_block
    return f"{base_description}\n\n{spec_block}"


# ==============================
# Scraping
# ==============================
@st.cache_resource(show_spinner=False)
def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_html(url):
    response = get_session().get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.url, response.text


def extract_json_ld(soup):
    items = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        cleaned = re.sub(r"^<!--|-->$", "", raw.strip())
        try:
            items.append(json.loads(cleaned))
        except Exception:
            continue
    return items


def flatten_json_ld(data):
    stack = [data]
    flattened = []
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            flattened.append(item)
            for key in ("@graph", "itemListElement", "offers"):
                if key in item:
                    stack.append(item[key])
    return flattened


def extract_price(soup, page_text, json_ld_items):
    def _price_from_value(value):
        price = parse_float(value)
        if price is not None and price > 0:
            return price
        return None

    def _price_from_spec(spec):
        if isinstance(spec, dict):
            for key in ("price", "salePrice", "currentPrice", "lowPrice", "highPrice"):
                price = _price_from_value(spec.get(key))
                if price is not None:
                    return price
        elif isinstance(spec, list):
            for item in spec:
                price = _price_from_spec(item)
                if price is not None:
                    return price
        return None

    def _price_from_offer(offer):
        if not isinstance(offer, dict):
            return None
        for key in ("price", "salePrice", "currentPrice"):
            price = _price_from_value(offer.get(key))
            if price is not None:
                return price
        price = _price_from_spec(offer.get("priceSpecification"))
        if price is not None:
            return price
        for key in ("lowPrice", "highPrice"):
            price = _price_from_value(offer.get(key))
            if price is not None:
                return price
        return None

    def _first_price_from_nodes(nodes):
        for node in nodes:
            if is_blocked_text_node(node):
                continue
            price = parse_float(node.get_text(" ", strip=True))
            if price is not None and price > 0:
                return price
        return None

    def _collect_nodes(scope, selectors):
        if scope is None:
            return []
        nodes = []
        for selector in selectors:
            nodes.extend(scope.select(selector))
        return nodes

    def _first_regex_price(text):
        normalized_page = safe_str(text).replace("\xa0", " ")
        text_patterns = (
            r"Aktualna cena wynosi:\s*([0-9\s\.]+,[0-9]{2})\s*zł",
            r"([0-9\s\.]+,[0-9]{2})\s*zł\s*Aktualna cena wynosi",
            r"cena\s*[:\-]?\s*([0-9\s\.]+,[0-9]{2})\s*zł",
            r"\b([0-9\s\.]+,[0-9]{2})\s*zł\b",
        )
        for pattern in text_patterns:
            match = re.search(pattern, normalized_page, flags=re.IGNORECASE)
            if match:
                price = parse_float(match.group(1))
                if price is not None and price > 0:
                    return price
        return None

    main_product = (
        soup.select_one("main .product.type-product")
        or soup.select_one(".single-product div.product")
        or soup.select_one("div.product.type-product")
        or soup.select_one("main div.product")
    )
    main_summary = main_product.select_one(".summary") if main_product else soup.select_one(".summary")
    main_scopes = [scope for scope in [main_summary, main_product] if scope is not None]

    selector_groups = [
        # WooCommerce current/sale price first
        [
            ".price ins .woocommerce-Price-amount bdi",
            ".price ins .woocommerce-Price-amount",
            "p.price ins .woocommerce-Price-amount bdi",
            "p.price ins .woocommerce-Price-amount",
        ],
        # Standard visible price
        [
            ".price .woocommerce-Price-amount bdi",
            ".price .woocommerce-Price-amount",
            "p.price .woocommerce-Price-amount bdi",
            "p.price .woocommerce-Price-amount",
            "span.price .woocommerce-Price-amount bdi",
            "span.price .woocommerce-Price-amount",
            ".price .amount bdi",
            ".price .amount",
        ],
    ]

    for selectors in selector_groups:
        for scope in main_scopes:
            price = _first_price_from_nodes(_collect_nodes(scope, selectors))
            if price is not None:
                return price

    # Last DOM-level fallback if product container is missing.
    for selectors in selector_groups:
        price = _first_price_from_nodes(_collect_nodes(soup, selectors))
        if price is not None:
            return price

    for data in json_ld_items:
        for item in flatten_json_ld(data):
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if isinstance(offers, dict):
                price = _price_from_offer(offers)
                if price is not None:
                    return price
            if isinstance(offers, list):
                for offer in offers:
                    price = _price_from_offer(offer)
                    if price is not None:
                        return price
            price = _price_from_spec(item.get("priceSpecification"))
            if price is not None:
                return price
            for key in ("price", "salePrice", "currentPrice", "lowPrice", "highPrice"):
                price = _price_from_value(item.get(key))
                if price is not None:
                    return price

    for attrs in (
        {"property": "product:price:amount"},
        {"name": "price"},
        {"itemprop": "price"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            price = parse_float(tag.get("content"))
            if price is not None and price > 0:
                return price

    # Regex fallback only on product-focused text (summary/product), not whole listing noise.
    if main_summary:
        price = _first_regex_price(main_summary.get_text(" ", strip=True))
        if price is not None:
            return price
    if main_product:
        price = _first_regex_price(main_product.get_text(" ", strip=True))
        if price is not None:
            return price

    # Page-level fallback only when there is exactly one unique price candidate.
    normalized_page = safe_str(page_text).replace("\xa0", " ")
    page_matches = re.findall(r"([0-9\s\.]+,[0-9]{2})\s*zł", normalized_page, flags=re.IGNORECASE)
    unique_prices = []
    seen_prices = set()
    for raw in page_matches:
        price = parse_float(raw)
        if price is None or price <= 0:
            continue
        if price not in seen_prices:
            seen_prices.add(price)
            unique_prices.append(price)
    if len(unique_prices) == 1:
        return unique_prices[0]

    # Fallback: inspect nearby price containers before taking any random amount from the page
    for selector in [".summary", "main .product.type-product", ".single-product div.product", "div.product.type-product"]:
        node = soup.select_one(selector)
        if not node:
            continue
        local_text = normalize_whitespace(node.get_text(" ", strip=True))
        matches = re.findall(r"([0-9\s\.]+,[0-9]{2})\s*zł", local_text, flags=re.IGNORECASE)
        prices = [parse_float(m) for m in matches]
        prices = [p for p in prices if p is not None and p > 0]
        if prices:
            return prices[0]

    return None


def extract_weight(soup, page_text, json_ld_items):
    for data in json_ld_items:
        for item in flatten_json_ld(data):
            weight = item.get("weight")
            if weight is not None:
                parsed = parse_float(weight)
                if parsed is not None:
                    return parsed
    for attrs in (
        {"property": "product:weight:value"},
        {"name": "weight"},
        {"itemprop": "weight"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            parsed = parse_float(tag.get("content"))
            if parsed is not None:
                return parsed
    for pattern in (
        r"waga\s*[:\-]?\s*(\d+[.,]?\d*)\s*kg",
        r"masa własna\s*(\d+[.,]?\d*)\s*kg",
        r"\b(\d+[.,]?\d*)\s*kg\b",
    ):
        match = re.search(pattern, page_text, flags=re.IGNORECASE)
        if match:
            parsed = parse_float(match.group(1))
            if parsed is not None:
                return parsed
    return None


def extract_availability(soup, json_ld_items, page_text):
    if soup.find(class_="out-of-stock"):
        return False
    if soup.find(class_="in-stock"):
        return True
    for data in json_ld_items:
        for item in flatten_json_ld(data):
            offers = item.get("offers")
            candidate = None
            if isinstance(offers, dict):
                candidate = safe_str(offers.get("availability"))
            elif isinstance(item.get("availability"), str):
                candidate = safe_str(item.get("availability"))
            if candidate:
                low = candidate.lower()
                if "instock" in low or "in_stock" in low:
                    return True
                if "outofstock" in low or "out_of_stock" in low:
                    return False
    lowered = page_text.lower()
    if any(x in lowered for x in ["brak w magazynie", "niedostępny", "niedostepny"]):
        return False
    if any(x in lowered for x in ["dostępny", "dostepny", "w magazynie"]):
        return True
    return None


def find_existing_description(soup):
    selectors = [
        ".woocommerce-product-details__short-description",
        ".product-short-description",
        ".short-description",
        ".description",
        "[itemprop='description']",
        ".product-description",
        ".entry-content",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = normalize_whitespace(node.get_text(" ", strip=True))
            if len(text) > 80:
                return text[:5000]
    paras = [normalize_whitespace(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
    paras = [p for p in paras if len(p) > 80]
    return "\n\n".join(paras[:4])[:5000]


def extract_images(soup, base_url):
    urls = []
    seen = set()
    for tag in soup.find_all(["img", "source"]):
        candidates = [tag.get("data-large_image"), tag.get("data-src"), tag.get("data-lazy-src"), tag.get("srcset"), tag.get("src")]
        for candidate in candidates:
            if not candidate:
                continue
            if "," in candidate and "http" in candidate:
                candidate = candidate.split(",")[0].split()[0]
            candidate = urljoin(base_url, candidate)
            low = candidate.lower()
            if not low.startswith(("http://", "https://")):
                continue
            if any(block in low for block in IMAGE_BLOCKLIST):
                continue
            if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", low) is None:
                continue
            if candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)
            if len(urls) >= MAX_IMAGES:
                return urls
    return urls


def extract_breadcrumb_category(soup):
    texts = []
    for selector in (".woocommerce-breadcrumb", ".breadcrumb", ".breadcrumbs"):
        node = soup.select_one(selector)
        if node:
            texts.append(normalize_whitespace(node.get_text(" ", strip=True)))
    joined = " | ".join(texts)
    low = ascii_fold(joined).lower()
    if "microcar" in low:
        return "MICROCAR"
    if "cross" in low or "enduro" in low:
        return "CROSS"
    if "skuter" in low:
        return "SKUTER"
    return joined[:200]


def extract_spec_value(page_text, label):
    pattern = rf"{label}\s+(.*?)\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ][^\n]*$|[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+\s+[A-ZĄĆĘŁŃÓŚŹŻ]|Opis|Specyfikacja|$)"
    m = re.search(pattern, page_text, flags=re.IGNORECASE)
    return normalize_whitespace(m.group(1)) if m else ""


BLACKLIST_TEXT_PATTERNS = [
    "zobacz rowniez", "zobacz również", "inne kategorie", "kategorie produktow", "kategorie produktów",
    "powiazane produkty", "powiązane produkty", "ostatnio ogladane", "ostatnio oglądane",
    "polecane produkty", "podobne produkty", "bestsellery", "koszyk", "moje konto",
]

BLOCKED_TEXT_TAGS = {"header", "footer", "nav"}
BLOCKED_TEXT_MARKERS = [
    "site-header",
    "site-footer",
    "sidebar",
    "widget",
    "related",
    "up-sells",
    "cross-sells",
    "woocommerce-breadcrumb",
    "posted_in",
    "tagged_as",
    "product-categories",
    "products columns-",
    "menu",
    "mega-menu",
    "breadcrumbs",
    "rank-math-breadcrumb",
    "storefront-breadcrumb",
]


def is_blocked_text_node(node):
    current = node
    while current is not None and getattr(current, "name", None):
        if current.name in BLOCKED_TEXT_TAGS:
            return True
        classes = " ".join(safe_str(c).lower() for c in current.get("class", []))
        marker_blob = f"{classes} {safe_str(current.get('id', '')).lower()}"
        if any(marker in marker_blob for marker in BLOCKED_TEXT_MARKERS):
            return True
        current = current.parent
    return False


def extract_product_main_text(soup):
    chunks = []
    selectors = [
        "main .product", ".single-product div.product", ".entry-summary", ".summary",
        ".woocommerce-product-details__short-description", ".woocommerce-Tabs-panel",
        "table", ".shop_attributes", ".product_meta",
    ]
    seen = set()
    for selector in selectors:
        for node in soup.select(selector):
            if is_blocked_text_node(node):
                continue
            txt = normalize_whitespace(node.get_text(" ", strip=True))
            if txt and txt not in seen:
                seen.add(txt)
                chunks.append(txt)

    if not chunks:
        fallback = soup.select_one("main") or soup.body
        if fallback:
            txt = normalize_whitespace(fallback.get_text(" ", strip=True))
            if txt:
                chunks.append(txt)

    text = normalize_whitespace(" ".join(chunks))
    low = ascii_fold(text).lower()
    for pattern in BLACKLIST_TEXT_PATTERNS:
        low = low.replace(ascii_fold(pattern).lower(), " ")
    return normalize_whitespace(low)


@st.cache_data(show_spinner=False, ttl=3600)
def scrape_product_url(url):
    final_url, html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    page_text = extract_product_main_text(soup)
    json_ld_items = extract_json_ld(soup)
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = normalize_whitespace(h1.get_text(" ", strip=True))
    if not title:
        title = normalize_whitespace(soup.title.get_text(" ", strip=True) if soup.title else "")
    sku_node = soup.find(class_="sku")
    sku = normalize_whitespace(sku_node.get_text(" ", strip=True)) if sku_node else ""
    breadcrumb = extract_breadcrumb_category(soup)
    tags = ", ".join(normalize_whitespace(a.get_text(" ", strip=True)) for a in soup.select(".tagged_as a, .product_meta .tagged_as a"))
    categories = ", ".join(normalize_whitespace(a.get_text(" ", strip=True)) for a in soup.select(".posted_in a, .product_meta .posted_in a"))
    weight = extract_weight(soup, page_text, json_ld_items)
    spec_fields = extract_spec_fields(soup, page_text, json_ld_items, weight=weight)
    vehicle_type = spec_fields.get("Rodzaj") or extract_spec_value(page_text, "Rodzaj")
    homologation = spec_fields.get("Homologacja") or extract_spec_value(page_text, "Homologacja")
    return {
        "url": final_url,
        "title": title,
        "page_text": page_text,
        "existing_description": find_existing_description(soup),
        "price": extract_price(soup, page_text, json_ld_items),
        "weight": weight,
        "available": extract_availability(soup, json_ld_items, page_text),
        "images": extract_images(soup, final_url),
        "sku": sku,
        "breadcrumb": breadcrumb,
        "source_category": categories,
        "tags": tags,
        "vehicle_type": vehicle_type,
        "homologation": homologation,
        "spec_fields": spec_fields,
    }


def detect_product_pattern(urls):
    buckets = defaultdict(list)
    for href in urls:
        parsed = urlparse(href)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            continue
        prefix = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        key = f"{parsed.scheme}://{parsed.netloc}{prefix}"
        buckets[key].append(href)
    if not buckets:
        return []
    _, candidates = max(buckets.items(), key=lambda item: len(set(item[1])))
    unique = list(dict.fromkeys(candidates))
    return unique if len(unique) >= 2 else []


def is_probable_product_url(href, base_url):
    if not href:
        return False
    href = urljoin(base_url, href)
    parsed = urlparse(href)
    if parsed.netloc != urlparse(base_url).netloc:
        return False
    low = href.lower()
    if "/produkt/" not in low:
        return False
    blocked = ["#", "javascript:", "/tag/", "/konto/", "/cart", "/koszyk", "/checkout", "/kontakt", "/blog/"]
    return not any(x in low for x in blocked)


@st.cache_data(show_spinner=False, ttl=3600)
def scrape_listing_products(url):
    final_url, html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    selectors = [
        "ul.products li a",
        ".products .product a",
        ".product-item a",
        ".product-grid a",
        ".woocommerce-loop-product__link",
        ".product a",
    ]
    links = []
    preview = []
    for selector in selectors:
        for a in soup.select(selector):
            href = a.get("href")
            if is_probable_product_url(href, final_url):
                full = urljoin(final_url, href)
                title = normalize_whitespace(a.get_text(" ", strip=True))
                if not title:
                    title = normalize_whitespace(a.get("title", ""))
                links.append(full)
                preview.append({"url": full, "title": title or full})
        unique = list(dict.fromkeys(links))
        if len(unique) >= 1:
            dedup_preview = []
            seen = set()
            for p in preview:
                if p["url"] in seen:
                    continue
                seen.add(p["url"])
                dedup_preview.append(p)
            return dedup_preview[:MAX_LISTING_PRODUCTS], f"CSS selectors ({selector})"

    json_ld_items = extract_json_ld(soup)
    json_links = []
    for data in json_ld_items:
        for item in flatten_json_ld(data):
            if item.get("@type") == "ItemList":
                for el in item.get("itemListElement", []):
                    if isinstance(el, dict):
                        href = el.get("url") or (el.get("item") or {}).get("url")
                        if is_probable_product_url(href, final_url):
                            full = urljoin(final_url, href)
                            title = ""
                            item_obj = el.get("item") or {}
                            if isinstance(item_obj, dict):
                                title = item_obj.get("name", "")
                            json_links.append({"url": full, "title": normalize_whitespace(title) or full})
    dedup = []
    seen = set()
    for p in json_links:
        if p["url"] in seen:
            continue
        seen.add(p["url"])
        dedup.append(p)
    if dedup:
        return dedup[:MAX_LISTING_PRODUCTS], "JSON-LD ItemList"

    all_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if is_probable_product_url(href, final_url):
            all_links.append(urljoin(final_url, href))
    detected = detect_product_pattern(all_links)
    if detected:
        return [{"url": u, "title": u} for u in detected[:MAX_LISTING_PRODUCTS]], "Pattern detection"
    return [], "No products detected"


def extract_json_from_response(text):
    text = safe_str(text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("Model nie zwrócił poprawnego JSON-a.")


# ==============================
# LLM
# ==============================
def image_to_claude_content(uploaded_file):
    if not uploaded_file:
        return []
    data = uploaded_file.getvalue()
    media_type = uploaded_file.type or "image/jpeg"
    return [{
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("utf-8"),
        },
    }]


def generate_with_claude(client, model, rewrite_mode, product_data, uploaded_image=None):
    if client is None:
        raise RuntimeError("Brak zainicjalizowanego klienta Claude API.")

    def _normalize_for_prompt(value):
        if isinstance(value, dict):
            cleaned = {}
            for key, val in value.items():
                cleaned[safe_str(key)] = _normalize_for_prompt(val)
            return cleaned
        if isinstance(value, list):
            return [_normalize_for_prompt(x) for x in value][:20]
        return safe_str(value)

    normalized = {}
    for k, v in product_data.items():
        normalized[k] = _normalize_for_prompt(v)
    prompt = {
        "instruction": "Wygeneruj zgodnie z system prompt. Zwróć wyłącznie JSON bez markdownu.",
        "mode": "REWRITE" if rewrite_mode else "CREATE",
        "product": normalized,
    }
    content = [{"type": "text", "text": json.dumps(prompt, ensure_ascii=False)}]
    content.extend(image_to_claude_content(uploaded_image))
    message = client.messages.create(
        model=model,
        max_tokens=2200,
        temperature=0.4,
        system=REWRITE_SYSTEM_PROMPT if rewrite_mode else CREATE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    data = extract_json_from_response(text)
    for field in ["name", "short_description", "description", "seo_title", "seo_description", "seo_url"]:
        data.setdefault(field, "")
    if not data["seo_url"]:
        data["seo_url"] = slugify(data.get("name") or normalized.get("name"))
    return data


# ==============================
# Category classification
# ==============================
def normalize_category(category):
    category = normalize_whitespace(category)
    return category.replace("Microcary", "Microcar")


def get_categories():
    unique = []
    seen = set()
    for cat in RAW_CATEGORIES:
        normalized = normalize_category(cat)
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


STORE_CATEGORIES = get_categories()

CLASSIFICATION_ALLOWED_CATEGORIES = [
    "Pojazdy elektryczne > Dla dzieci i młodzieży",
    "Pojazdy elektryczne > Motocykle 50 cc",
    "Pojazdy elektryczne > Motocykle 125 cc",
    "Pojazdy elektryczne > Skutery 50 cc",
    "Pojazdy elektryczne > Skutery 125 cc",
    "Pojazdy elektryczne > Cross / Enduro",
    "Pojazdy elektryczne > Quady",
    "Pojazdy elektryczne > Hulajnogi",
    "Pojazdy elektryczne > Delivery i Cargo",
    "Pojazdy elektryczne > Inwalidzkie",
    "Pojazdy elektryczne > Golfowe",
    "Pojazdy elektryczne > Microcar",
    "Skutery elektryczne > Skuter 50 cm³ (L1e)",
    "Skutery elektryczne > Skuter 125 cm³ (L3e)",
    "Motocykle enduro elektryczne > Mini cross elektryczny dla dzieci i młodzieży",
    "Motocykle enduro elektryczne > L1e / do 50 cm³",
    "Motocykle enduro elektryczne > L3e / do 125 cm³",
    "Motocykle enduro elektryczne > Off-road / bez homologacji (full cross)",
    "Części i akcesoria > Pielęgnacja Motocykla",
    "Części i akcesoria > Personalizacja > Okleina Surron Ultra Bee",
    "Części i akcesoria > Personalizacja > Okleina Surron Light Bee",
    "Części i akcesoria > Personalizacja > Okleina Talaria MX3/MX4",
    "Części i akcesoria > Personalizacja > Projekt customowy",
    "Części i akcesoria > Personalizacja > Siedzenia",
    "Części i akcesoria > Personalizacja > Koła",
    "Części zamienne",
    "Części zamienne > Części zamienne eRide Pro > Baterie i ładowarki",
    "Części zamienne > Części zamienne eRide Pro > Elektronika i sterowanie",
    "Części zamienne > Części zamienne eRide Pro > Silnik i układ napędowy",
    "Części zamienne > Części zamienne eRide Pro > Kierownica i sterowanie",
    "Modyfikacje > Akumulatory",
    "Modyfikacje > Surron Light Bee",
    "Modyfikacje > Akcesoria TORP",
    "Modyfikacje > Silniki TORP",
    "Akcesoria > Gogle motocyklowe > Gogle crossowe",
    "Odzież > Kombinezony",
    "Wyjazdy",
]
CLASSIFICATION_ALLOWED_SET = set(CLASSIFICATION_ALLOWED_CATEGORIES)
VEHICLE_CATEGORY_SET = {
    "Pojazdy elektryczne > Dla dzieci i młodzieży",
    "Pojazdy elektryczne > Motocykle 50 cc",
    "Pojazdy elektryczne > Motocykle 125 cc",
    "Pojazdy elektryczne > Skutery 50 cc",
    "Pojazdy elektryczne > Skutery 125 cc",
    "Pojazdy elektryczne > Cross / Enduro",
    "Pojazdy elektryczne > Quady",
    "Pojazdy elektryczne > Hulajnogi",
    "Pojazdy elektryczne > Delivery i Cargo",
    "Pojazdy elektryczne > Inwalidzkie",
    "Pojazdy elektryczne > Golfowe",
    "Pojazdy elektryczne > Microcar",
    "Skutery elektryczne > Skuter 50 cm³ (L1e)",
    "Skutery elektryczne > Skuter 125 cm³ (L3e)",
    "Motocykle enduro elektryczne > Mini cross elektryczny dla dzieci i młodzieży",
    "Motocykle enduro elektryczne > L1e / do 50 cm³",
    "Motocykle enduro elektryczne > L3e / do 125 cm³",
    "Motocykle enduro elektryczne > Off-road / bez homologacji (full cross)",
}
NON_VEHICLE_CATEGORY_SET = CLASSIFICATION_ALLOWED_SET - VEHICLE_CATEGORY_SET


CHILDREN_TERMS = ["dzieci", "dziecko", "dzieciecy", "dziecieca", "mlodziez", "mlodziezowy", "junior", "kids", "youth", "balance bike", "rowerek biegowy"]
ENDURO_TERMS = ["cross", "motocross", "pitbike", "pit bike", "enduro", "dirt bike", "full cross"]
MOTORCYCLE_TERMS = ["motocykl", "motor", "supermoto", "naked"]
SCOOTER_TERMS = ["skuter", "scooter"]
MICROCAR_TERMS = ["microcar", "mikrosamochod", "micro samochod", "samochod elektryczny", "czterokolowiec", "czterokołowiec", "lekki samochod", "lekki samochód"]
QUAD_TERMS = ["quad", "atv"]
MINI_CROSS_TERMS = ["pitbike", "pit bike", "mini cross", "mini-cross", "minicross", "mini bike", "minibike", "cross junior", "junior cross"]
ADULT_USAGE_TERMS = [
    "dla doroslych",
    "dla dorosłych",
    "pelnowymiar",
    "pełnowymiar",
    "full size",
    "full-size",
    "do ruchu drogowego",
    "dopuszczony do ruchu",
    "homologacja drogowa",
    "motocykl drogowy",
    "adventure",
    "dual sport",
]
ROAD_HOMO_CODES = ["l1e", "l3e", "l6e", "l7e"]
CATEGORY_CONFIDENCE_THRESHOLD = 0.60


def infer_wheel_count(text, vehicle_type=""):
    blob = ascii_fold(" ".join([safe_str(text), safe_str(vehicle_type)])).lower()
    if any(term in blob for term in ["czterokolowiec", "czterokolowy", "4 kol", "4-ko", "quad", "atv", "microcar"]):
        return 4
    if any(term in blob for term in ["trojkol", "trójkol", "3 kol"]):
        return 3
    if any(term in blob for term in ["hulajn", "rower", "rowerek", "motocykl", "skuter", "cross", "enduro", "pitbike", "pit bike"]):
        return 2
    return None


def infer_tire_signature(text):
    pairs = infer_tire_pairs(text)
    if not pairs:
        return ""
    return ", ".join([f"{a}/{b}" for a, b in pairs[:4]])


def infer_tire_pairs(text):
    blob = normalize_whitespace(safe_str(text)).lower()
    pairs = []
    # Keep only sizes that look like tire notation and appear near wheel/tire context.
    for match in re.finditer(r"\b(\d{1,2})\s*[/x]\s*(\d{1,2})\b", blob):
        left, right = match.group(1), match.group(2)
        context_window = blob[max(0, match.start() - 24):match.end() + 24]
        if not has_any(context_window, ["opon", "kolo", "koło", "kol", "wheel", "felg"]):
            continue
        try:
            left_i, right_i = int(left), int(right)
            if left_i < 8 or right_i < 8:
                continue
            if left_i > 24 or right_i > 24:
                continue
            pairs.append((left_i, right_i))
        except Exception:
            continue
        if len(pairs) >= 8:
            break
    return pairs


def extract_homologation_codes(homologation_text="", product_text=""):
    blob = ascii_fold(" ".join([safe_str(homologation_text), safe_str(product_text)])).lower()
    compact = re.sub(r"[^a-z0-9]", "", blob)
    codes = set()
    for code in ROAD_HOMO_CODES:
        if code in compact:
            codes.add(code)
    return codes


def infer_power_watts(text):
    blob = normalize_whitespace(safe_str(text)).lower().replace(",", ".")
    watts = []
    for raw in re.findall(r"\b(\d+(?:\.\d+)?)\s*kw\b", blob):
        try:
            watts.append(float(raw) * 1000.0)
        except Exception:
            continue
    for raw in re.findall(r"\b(\d{3,5})\s*w\b", blob):
        try:
            watts.append(float(raw))
        except Exception:
            continue
    if not watts:
        return None
    return max(watts)


def infer_cc_equivalent(text):
    blob = ascii_fold(safe_str(text)).lower().replace(" ", "")
    if any(x in blob for x in ["125cc", "125cm3", "125cm", "do125cm"]):
        return 125
    if any(x in blob for x in ["50cc", "50cm3", "50cm", "do50cm"]):
        return 50
    return None


def has_any(text, terms):
    blob = ascii_fold(safe_str(text)).lower()
    return any(ascii_fold(term).lower() in blob for term in terms)


def count_any(text, terms):
    blob = ascii_fold(safe_str(text)).lower()
    score = 0
    for term in terms:
        token = ascii_fold(term).lower()
        if token and token in blob:
            score += 1
    return score


def category_decision(category="", confidence=0.0, reason="", signals=None):
    resolved_category = category if category in CLASSIFICATION_ALLOWED_SET else ""
    return {
        "category": resolved_category,
        "confidence": float(confidence or 0.0),
        "reason": safe_str(reason),
        "signals": signals or {},
    }


def classify_vehicle_category_detailed(title="", description="", page_text="", manual_category="", source_category="", breadcrumb="", tags="", vehicle_type="", homologation="", weight=None):
    manual_category = normalize_category(manual_category)
    if manual_category in CLASSIFICATION_ALLOWED_SET:
        return category_decision(manual_category, 1.0, "manual_category")

    # Product-centric text only: avoids menu/listing noise as much as possible.
    product_focus_content = " ".join([title or "", description or "", tags or "", vehicle_type or "", homologation or ""])
    product_content = " ".join([product_focus_content, page_text or ""])
    context_content = " ".join([source_category or "", breadcrumb or ""])
    product_text = ascii_fold(product_content).lower()
    product_focus_text = ascii_fold(product_focus_content).lower()
    context_text = ascii_fold(context_content).lower()
    title_text = ascii_fold(title).lower()
    title_motorcycle_signal = has_any(title_text, ["motocykl", "motorcycle", "supermoto"]) or re.search(r"\bmoto\b", title_text) is not None
    title_scooter_signal = has_any(title_text, ["skuter", "scooter"])
    title_quad_signal = has_any(title_text, ["quad", "atv"])
    title_microcar_signal = has_any(title_text, ["microcar", "mikrosamochod", "samochod elektryczny"])

    homologation_codes = extract_homologation_codes(homologation, product_content)
    has_l1e = "l1e" in homologation_codes
    has_l3e = "l3e" in homologation_codes
    has_l6e = "l6e" in homologation_codes
    has_l7e = "l7e" in homologation_codes
    homologation_raw_text = ascii_fold(homologation).lower()
    homologation_yes_field = bool(re.search(r"\b(tak|yes|road legal|street legal|homologacja drogowa)\b", homologation_raw_text))
    homologation_no_field = bool(re.search(r"\b(nie|no|brak|bez homologacji|off-?road|tylko tor)\b", homologation_raw_text))
    homologation_declared = has_any(product_focus_text, [
        "homologacja drogowa",
        "dopuszczony do ruchu",
        "legalnie po drogach",
        "legalnego poruszania sie po drogach",
    ]) or homologation_yes_field
    no_homologation_declared = has_any(product_focus_text, [
        "bez homologacji",
        "brak homologacji",
        "off-road only",
        "tylko tor",
        "niedopuszczony do ruchu",
    ]) or homologation_no_field
    road_homologation = bool(homologation_codes) or (homologation_declared and not no_homologation_declared)
    strong_road_homologation = has_l3e or has_l6e or has_l7e or (has_l1e and road_homologation)

    wheel_count = infer_wheel_count(product_focus_content, vehicle_type) or infer_wheel_count(product_content, vehicle_type)
    tire_signature = infer_tire_signature(product_focus_content) or infer_tire_signature(product_content)
    tire_pairs = infer_tire_pairs(product_focus_content) or infer_tire_pairs(product_content)
    weight_value = parse_float(weight)
    power_watts = infer_power_watts(product_content)
    cc_equivalent = infer_cc_equivalent(product_content)

    tire_max = max((max(left, right) for left, right in tire_pairs), default=None)
    small_wheel_signal = tire_max is not None and tire_max <= 14
    large_wheel_signal = tire_max is not None and tire_max >= 17

    very_low_weight_signal = weight_value is not None and weight_value <= 45
    low_weight_signal = weight_value is not None and weight_value <= 55
    adult_weight_signal = weight_value is not None and weight_value >= 65
    high_weight_signal = weight_value is not None and weight_value >= 75

    explicit_child_target = (
        has_any(title_text, ["dziecie", "junior", "youth", "kids"])
        or has_any(product_focus_text, [
            "dla dzieci",
            "dla mlodziezy",
            "dla młodzieży",
            "dzieciecy",
            "dziecięcy",
            "dziecieca",
            "dziecięca",
            "dla juniorow",
            "dla juniorów",
            "for youth",
            "for kids",
            "pitbike dzieci",
            "mini cross dla dzieci",
        ])
    )
    explicit_mini_cross = has_any(product_focus_text, MINI_CROSS_TERMS)
    child_age_signal = re.search(r"\b(?:[3-9]|1[0-6])\s*(?:lat|yo)\b", product_focus_text) is not None

    terrain_signal = has_any(product_focus_text, ["teren", "terenow", "trail"])
    offroad_signal = has_any(product_focus_text, ["off-road", "offroad", "bez homologacji", "full cross", "tylko tor"])
    enduro_primary_hits = count_any(product_focus_text, ["enduro", "cross", "motocross", "pitbike", "pit bike", "off-road", "full cross", "trail"])
    enduro_secondary_hits = count_any(product_text, ["enduro", "cross", "motocross", "pitbike", "pit bike", "off-road", "full cross", "trail"])
    enduro_strength = (enduro_primary_hits * 2) + min(enduro_secondary_hits, 2)
    enduro_signal = (
        enduro_primary_hits >= 1
        or offroad_signal
        or (enduro_secondary_hits >= 2 and (terrain_signal or offroad_signal))
    )
    moto_signal = title_motorcycle_signal or has_any(product_focus_text, MOTORCYCLE_TERMS + ["szos", "miejsk", "drogow", "commuter", "street", "touring"])
    standing_scooter_signal = has_any(product_focus_text, ["hulajn", "standing scooter", "e-scooter", "escooter"])
    scooter_signal = (title_scooter_signal or has_any(product_focus_text, SCOOTER_TERMS)) and not standing_scooter_signal
    quad_signal = title_quad_signal or has_any(product_focus_text, QUAD_TERMS + ["czterokolowy terenowy", "czterokołowy terenowy"])
    microcar_word_signal = title_microcar_signal or has_any(product_focus_text, MICROCAR_TERMS + ["mikrosamochod", "samochod elektryczny"])
    cargo_core_signal = has_any(product_focus_text, [
        "cargo",
        "delivery box",
        "box delivery",
        "pojazd dostawczy",
        "dostawczy",
        "skrzynia ladunkowa",
        "skrzynia ładunkowa",
        "przewoz towar",
        "przewóz towar",
        "transport towar",
    ])
    service_delivery_signal = has_any(product_focus_text, [
        "szybka dostawa",
        "door-to-door",
        "dostawa i",
        "wysylka",
        "wysyłka",
        "kurier",
        "zamowienie",
        "zamówienie",
    ])
    cargo_signal = cargo_core_signal and not (service_delivery_signal and not has_any(product_focus_text, ["pojazd dostawczy", "cargo", "skrzynia ladunkowa", "skrzynia ładunkowa"]))
    mobility_signal = has_any(product_focus_text, ["inwalidz", "rehabil", "mobility scooter", "ograniczona mobilnosc", "ograniczona mobilność"])
    golf_signal = has_any(product_focus_text, ["golf", "golf cart", "pole golfowe"])
    explicit_adult_usage = has_any(product_focus_text, ADULT_USAGE_TERMS + ["dla doroslych", "dla dorosłych"])

    goggles_signal = has_any(product_focus_text, ["gogle", "goggle", "goggles"])
    suit_signal = has_any(product_focus_text, ["kombinezon", "riding suit", "odziez motocyklowa", "odzież motocyklowa"])
    trip_signal = has_any(product_focus_text, ["wyjazd", "wypraw", "event", "oboz", "obóz", "szkolenie"])
    care_signal = has_any(product_focus_text, ["pielegn", "pielęgn", "czyszc", "cleaner", "detailing", "smar", "wosk", "poler", "impregnat"])
    wrap_signal = has_any(product_focus_text, ["okleina", "wrap", "folia", "folie"])
    seat_signal = has_any(product_focus_text, ["siedzenie", "seat"])
    wheel_accessory_signal = has_any(product_focus_text, ["felga", "obręcz", "obrecz", "piasta", "szprycha", "zestaw kol", "zestaw koł", "wheel set"])
    custom_project_signal = has_any(product_focus_text, ["projekt custom", "customowy", "indywidualny projekt"])
    eride_pro_signal = has_any(product_focus_text, ["eride pro", "e ride pro", "eridepro"])
    spare_part_signal = has_any(product_focus_text, ["czesc", "część", "zamienn", "replacement", "spare part", "zestaw naprawczy", "komponent", "element"])
    mod_signal = has_any(product_focus_text, ["modyfik", "upgrade", "tuning", "performance", "kit"])
    torp_signal = has_any(product_focus_text, ["torp"])
    torp_motor_signal = torp_signal and has_any(product_focus_text, ["silnik", "motor"])
    battery_signal = has_any(product_focus_text, ["akumulator", "battery", "bateria", "pakiet", "ladowarka", "ładowarka"])
    battery_upgrade_signal = battery_signal and mod_signal

    conflict_rejections = {}
    motorcycle_title_priority = bool(title_motorcycle_signal and not title_quad_signal and not title_microcar_signal)

    strong_4w_signals = any([
        has_l6e,
        has_l7e,
        title_quad_signal,
        title_microcar_signal,
        has_any(product_text, ["czterokolowiec", "czterokołowiec"]),
    ])
    if motorcycle_title_priority and wheel_count == 4 and not strong_4w_signals:
        wheel_count = 2
        conflict_rejections["wheel_count"] = "wheel_count_4_rejected_by_motorcycle_title_priority"

    strong_mobility_support = has_any(product_focus_text, ["skuter inwalidzki", "wozek inwalidzki", "wózek inwalidzki", "rehabilitacyjny", "dla osob z ograniczona mobilnoscia", "dla osob z ograniczona mobilnością"])
    strong_cargo_support = cargo_core_signal and has_any(product_focus_text, ["pojazd dostawczy", "skrzynia ladunkowa", "skrzynia ładunkowa", "box delivery", "delivery box"])
    strong_quad_support = title_quad_signal or ((wheel_count == 4) and has_any(product_focus_text, ["quad", "atv", "czterokolowy terenowy", "czterokołowy terenowy"]))
    strong_microcar_support = has_l6e or has_l7e or (title_microcar_signal and wheel_count == 4)
    strong_standing_scooter_support = has_any(title_text, ["hulajnoga", "e-scooter", "escooter"]) and standing_scooter_signal

    strong_non_motorcycle_override = any([
        strong_mobility_support and not road_homologation and not moto_signal,
        strong_cargo_support and not road_homologation and not moto_signal,
        strong_quad_support and not road_homologation and not moto_signal,
        strong_microcar_support,
        strong_standing_scooter_support and not road_homologation and not moto_signal,
    ])

    if motorcycle_title_priority and not strong_non_motorcycle_override:
        if mobility_signal:
            mobility_signal = False
            conflict_rejections["mobility"] = "rejected_by_motorcycle_title_priority"
        if cargo_signal:
            cargo_signal = False
            conflict_rejections["cargo"] = "rejected_by_motorcycle_title_priority"
        if quad_signal:
            quad_signal = False
            conflict_rejections["quad"] = "rejected_by_motorcycle_title_priority"
        if microcar_word_signal:
            microcar_word_signal = False
            conflict_rejections["microcar"] = "rejected_by_motorcycle_title_priority"
        if standing_scooter_signal:
            standing_scooter_signal = False
            conflict_rejections["standing_scooter"] = "rejected_by_motorcycle_title_priority"

    strong_title_enduro = has_any(title_text, ["enduro", "cross", "motocross", "pitbike", "trail"])
    prefer_motorcycle_family = (
        motorcycle_title_priority
        and road_homologation
        and not no_homologation_declared
        and not strong_title_enduro
        and enduro_strength < 4
    )
    if prefer_motorcycle_family and enduro_signal:
        conflict_rejections["enduro"] = "rejected_enduro_by_road_motorcycle_title_priority"

    vehicle_type_score = sum([
        int(enduro_signal),
        int(moto_signal),
        int(scooter_signal),
        int(standing_scooter_signal),
        int(quad_signal),
        int(microcar_word_signal),
        int(cargo_signal),
        int(mobility_signal),
        int(golf_signal),
    ])
    technical_vehicle_score = sum([
        int(bool(homologation_codes)),
        int(wheel_count in (2, 3, 4)),
        int(bool(tire_pairs)),
        int(weight_value is not None and weight_value >= 35),
        int(power_watts is not None),
        int(cc_equivalent is not None),
    ])
    hard_technical_vehicle_score = sum([
        int(bool(homologation_codes)),
        int(road_homologation),
        int(bool(tire_pairs)),
        int(weight_value is not None and weight_value >= 35),
        int(power_watts is not None),
        int(cc_equivalent is not None),
    ])
    part_type_score = sum([
        int(goggles_signal),
        int(suit_signal),
        int(trip_signal),
        int(care_signal),
        int(wrap_signal),
        int(seat_signal),
        int(wheel_accessory_signal),
        int(eride_pro_signal),
        int(spare_part_signal),
        int(mod_signal),
        int(torp_signal),
    ])
    full_vehicle_signal = (
        bool(homologation_codes)
        or (vehicle_type_score >= 2 and hard_technical_vehicle_score >= 1)
        or (vehicle_type_score >= 1 and hard_technical_vehicle_score >= 2)
        or (wheel_count in (2, 4) and weight_value is not None and weight_value >= 45)
        or (power_watts is not None and power_watts >= 2000 and vehicle_type_score >= 1)
    )
    if part_type_score >= 2 and vehicle_type_score == 0 and not bool(homologation_codes):
        full_vehicle_signal = False

    detected_product_family = "vehicle" if full_vehicle_signal else "part_accessory" if part_type_score >= 1 else "unknown"

    adult_blockers_for_child = any([
        high_weight_signal,
        strong_road_homologation,
        large_wheel_signal,
        explicit_adult_usage,
        power_watts is not None and power_watts >= 5000,
    ])
    child_profile_points = sum([
        int(explicit_child_target),
        int(explicit_mini_cross),
        int(child_age_signal),
        int(very_low_weight_signal),
        int(low_weight_signal),
        int(small_wheel_signal),
    ])
    child_profile_strong = (
        (explicit_child_target or explicit_mini_cross)
        and (very_low_weight_signal or low_weight_signal or small_wheel_signal)
        and child_profile_points >= 3
        and not adult_blockers_for_child
    )
    no_homologation_signal = (offroad_signal or no_homologation_declared) and not road_homologation

    signals = {
        "detected_product_family": detected_product_family,
        "detected_vehicle_type": "unknown",
        "title_motorcycle_signal": title_motorcycle_signal,
        "title_scooter_signal": title_scooter_signal,
        "title_quad_signal": title_quad_signal,
        "title_microcar_signal": title_microcar_signal,
        "motorcycle_title_priority": motorcycle_title_priority,
        "strong_non_motorcycle_override": strong_non_motorcycle_override,
        "prefer_motorcycle_family": prefer_motorcycle_family,
        "homologation_raw": ascii_fold(homologation).lower(),
        "homologation_codes": sorted(homologation_codes),
        "road_homologation": road_homologation,
        "strong_road_homologation": strong_road_homologation,
        "homologation_declared": homologation_declared,
        "no_homologation_declared": no_homologation_declared,
        "no_homologation_signal": no_homologation_signal,
        "wheel_count": wheel_count,
        "weight": weight_value,
        "power_watts": power_watts,
        "cc_equivalent": cc_equivalent,
        "tire_signature": tire_signature,
        "tire_pairs": [f"{left}/{right}" for left, right in tire_pairs],
        "small_wheel_signal": small_wheel_signal,
        "large_wheel_signal": large_wheel_signal,
        "very_low_weight_signal": very_low_weight_signal,
        "low_weight_signal": low_weight_signal,
        "adult_weight_signal": adult_weight_signal,
        "high_weight_signal": high_weight_signal,
        "vehicle_type_score": vehicle_type_score,
        "technical_vehicle_score": technical_vehicle_score,
        "hard_technical_vehicle_score": hard_technical_vehicle_score,
        "part_type_score": part_type_score,
        "full_vehicle_signal": full_vehicle_signal,
        "explicit_child_target": explicit_child_target,
        "explicit_mini_cross": explicit_mini_cross,
        "child_age_signal": child_age_signal,
        "child_profile_points": child_profile_points,
        "child_profile_strong": child_profile_strong,
        "adult_blockers_for_child": adult_blockers_for_child,
        "explicit_adult_usage": explicit_adult_usage,
        "enduro_signal": enduro_signal,
        "enduro_strength": enduro_strength,
        "terrain_signal": terrain_signal,
        "offroad_signal": offroad_signal,
        "moto_signal": moto_signal,
        "scooter_signal": scooter_signal,
        "standing_scooter_signal": standing_scooter_signal,
        "quad_signal": quad_signal,
        "microcar_word_signal": microcar_word_signal,
        "cargo_signal": cargo_signal,
        "cargo_core_signal": cargo_core_signal,
        "service_delivery_signal": service_delivery_signal,
        "mobility_signal": mobility_signal,
        "golf_signal": golf_signal,
        "strong_mobility_support": strong_mobility_support,
        "strong_cargo_support": strong_cargo_support,
        "strong_quad_support": strong_quad_support,
        "strong_microcar_support": strong_microcar_support,
        "strong_standing_scooter_support": strong_standing_scooter_support,
        "conflict_rejections": conflict_rejections,
        "part_signal": part_type_score >= 1,
        "rejected_children_reason": "",
        "rejected_pitbike_reason": "",
        "rejected_microcar_reason": "",
    }

    # Layer 1: non-vehicle categories (only if no strong vehicle profile).
    if detected_product_family != "vehicle":
        if goggles_signal:
            signals["detected_vehicle_type"] = "accessory_goggles"
            return category_decision("Akcesoria > Gogle motocyklowe > Gogle crossowe", 0.97, "non_vehicle_goggles", signals)
        if suit_signal:
            signals["detected_vehicle_type"] = "clothing_suit"
            return category_decision("Odzież > Kombinezony", 0.96, "non_vehicle_suit", signals)
        if trip_signal:
            signals["detected_vehicle_type"] = "trip_service"
            return category_decision("Wyjazdy", 0.95, "non_vehicle_trip", signals)

        if wrap_signal and has_any(product_text, ["ultra bee"]):
            signals["detected_vehicle_type"] = "wrap_ultra_bee"
            return category_decision("Części i akcesoria > Personalizacja > Okleina Surron Ultra Bee", 0.95, "non_vehicle_wrap_ultra_bee", signals)
        if wrap_signal and has_any(product_text, ["light bee"]):
            signals["detected_vehicle_type"] = "wrap_light_bee"
            return category_decision("Części i akcesoria > Personalizacja > Okleina Surron Light Bee", 0.95, "non_vehicle_wrap_light_bee", signals)
        if wrap_signal and has_any(product_text, ["talaria", "mx3", "mx4"]):
            signals["detected_vehicle_type"] = "wrap_talaria"
            return category_decision("Części i akcesoria > Personalizacja > Okleina Talaria MX3/MX4", 0.95, "non_vehicle_wrap_talaria", signals)
        if custom_project_signal:
            signals["detected_vehicle_type"] = "custom_project"
            return category_decision("Części i akcesoria > Personalizacja > Projekt customowy", 0.88, "non_vehicle_custom_project", signals)
        if seat_signal:
            signals["detected_vehicle_type"] = "seat_accessory"
            return category_decision("Części i akcesoria > Personalizacja > Siedzenia", 0.9, "non_vehicle_seat", signals)
        if wheel_accessory_signal:
            signals["detected_vehicle_type"] = "wheel_accessory"
            return category_decision("Części i akcesoria > Personalizacja > Koła", 0.88, "non_vehicle_wheels", signals)
        if care_signal:
            signals["detected_vehicle_type"] = "care_product"
            return category_decision("Części i akcesoria > Pielęgnacja Motocykla", 0.9, "non_vehicle_care", signals)

        if torp_motor_signal:
            signals["detected_vehicle_type"] = "torp_motor"
            return category_decision("Modyfikacje > Silniki TORP", 0.94, "non_vehicle_torp_motor", signals)
        if torp_signal:
            signals["detected_vehicle_type"] = "torp_accessories"
            return category_decision("Modyfikacje > Akcesoria TORP", 0.9, "non_vehicle_torp_accessories", signals)

        if eride_pro_signal and has_any(product_text, ["bateria", "akumulator", "ladowarka", "ładowarka", "charger"]):
            signals["detected_vehicle_type"] = "eride_parts_battery"
            return category_decision("Części zamienne > Części zamienne eRide Pro > Baterie i ładowarki", 0.95, "non_vehicle_eride_battery", signals)
        if eride_pro_signal and has_any(product_text, ["elektronika", "sterownik", "kontroler", "controller", "display", "modul", "moduł"]):
            signals["detected_vehicle_type"] = "eride_parts_electronics"
            return category_decision("Części zamienne > Części zamienne eRide Pro > Elektronika i sterowanie", 0.93, "non_vehicle_eride_electronics", signals)
        if eride_pro_signal and has_any(product_text, ["silnik", "naped", "napęd", "przeniesienie napedu", "łańcuch", "lancuch"]):
            signals["detected_vehicle_type"] = "eride_parts_drivetrain"
            return category_decision("Części zamienne > Części zamienne eRide Pro > Silnik i układ napędowy", 0.93, "non_vehicle_eride_drivetrain", signals)
        if eride_pro_signal and has_any(product_text, ["kierownica", "manetka", "sterowanie", "hamulec", "dzwignia", "dźwignia"]):
            signals["detected_vehicle_type"] = "eride_parts_steering"
            return category_decision("Części zamienne > Części zamienne eRide Pro > Kierownica i sterowanie", 0.93, "non_vehicle_eride_steering", signals)

        if mod_signal and has_any(product_text, ["surron light bee"]):
            signals["detected_vehicle_type"] = "mod_surron_light_bee"
            return category_decision("Modyfikacje > Surron Light Bee", 0.9, "non_vehicle_mod_surron_light_bee", signals)
        if battery_upgrade_signal:
            signals["detected_vehicle_type"] = "battery_mod"
            return category_decision("Modyfikacje > Akumulatory", 0.86, "non_vehicle_battery_mod", signals)
        if spare_part_signal or eride_pro_signal:
            signals["detected_vehicle_type"] = "generic_spare_part"
            return category_decision("Części zamienne", 0.78, "non_vehicle_spare_part", signals)
        return category_decision("", 0.0, "non_vehicle_low_confidence", signals)

    # Layer 2: detect full-vehicle type.
    if mobility_signal:
        signals["detected_vehicle_type"] = "mobility"
        return category_decision("Pojazdy elektryczne > Inwalidzkie", 0.94, "vehicle_mobility_profile", signals)
    if golf_signal:
        signals["detected_vehicle_type"] = "golf"
        return category_decision("Pojazdy elektryczne > Golfowe", 0.93, "vehicle_golf_profile", signals)
    if cargo_signal:
        signals["detected_vehicle_type"] = "cargo_delivery"
        return category_decision("Pojazdy elektryczne > Delivery i Cargo", 0.92, "vehicle_cargo_profile", signals)
    if standing_scooter_signal and not scooter_signal:
        signals["detected_vehicle_type"] = "standing_scooter"
        return category_decision("Pojazdy elektryczne > Hulajnogi", 0.92, "vehicle_hulajnoga_profile", signals)

    microcar_strong_signal = (
        has_l6e
        or has_l7e
        or (wheel_count == 4 and microcar_word_signal and not quad_signal)
    )
    signals["microcar_strong_signal"] = microcar_strong_signal
    if microcar_strong_signal and not (enduro_signal or moto_signal):
        signals["detected_vehicle_type"] = "microcar"
        return category_decision("Pojazdy elektryczne > Microcar", 0.97 if (has_l6e or has_l7e) else 0.9, "vehicle_microcar_strong_signals", signals)
    if microcar_word_signal and not microcar_strong_signal:
        signals["rejected_microcar_reason"] = "missing_strong_signals_l6e_l7e_or_4wheel_car_profile"

    if quad_signal and wheel_count == 4 and not microcar_strong_signal:
        signals["detected_vehicle_type"] = "quad"
        return category_decision("Pojazdy elektryczne > Quady", 0.9, "vehicle_quad_profile", signals)

    if scooter_signal:
        signals["detected_vehicle_type"] = "scooter"
        if has_l3e:
            return category_decision("Skutery elektryczne > Skuter 125 cm³ (L3e)", 0.97, "scooter_l3e_homologation", signals)
        if has_l1e:
            return category_decision("Skutery elektryczne > Skuter 50 cm³ (L1e)", 0.96, "scooter_l1e_homologation", signals)
        if cc_equivalent == 125 or (power_watts is not None and power_watts >= 5000):
            return category_decision("Pojazdy elektryczne > Skutery 125 cc", 0.8, "scooter_125_profile", signals)
        if cc_equivalent == 50 or (power_watts is not None and power_watts <= 4000 and not high_weight_signal):
            return category_decision("Pojazdy elektryczne > Skutery 50 cc", 0.78, "scooter_50_profile", signals)
        return category_decision("", 0.0, "scooter_ambiguous_without_technical_discriminator", signals)

    if enduro_signal and not prefer_motorcycle_family:
        signals["detected_vehicle_type"] = "enduro"
        if explicit_child_target or explicit_mini_cross:
            if child_profile_strong:
                return category_decision(
                    "Motocykle enduro elektryczne > Mini cross elektryczny dla dzieci i młodzieży",
                    0.97,
                    "enduro_mini_cross_strong_child_profile",
                    signals,
                )
            reject_reason = "child_keywords_without_child_technical_profile"
            if adult_blockers_for_child:
                reject_reason = "adult_enduro_profile_blocks_child_category"
            signals["rejected_children_reason"] = reject_reason
            signals["rejected_pitbike_reason"] = reject_reason

        if has_l3e:
            return category_decision("Motocykle enduro elektryczne > L3e / do 125 cm³", 0.97, "enduro_l3e_homologation", signals)
        if has_l1e:
            return category_decision("Motocykle enduro elektryczne > L1e / do 50 cm³", 0.95, "enduro_l1e_homologation", signals)
        if offroad_signal and not road_homologation:
            return category_decision(
                "Motocykle enduro elektryczne > Off-road / bez homologacji (full cross)",
                0.94,
                "enduro_offroad_without_homologation",
                signals,
            )
        if adult_weight_signal or large_wheel_signal or explicit_adult_usage or enduro_strength >= 2:
            return category_decision("Pojazdy elektryczne > Cross / Enduro", 0.86, "enduro_general_adult_profile", signals)
        return category_decision("", 0.0, "enduro_low_confidence", signals)

    if moto_signal or (wheel_count == 2 and detected_product_family == "vehicle"):
        signals["detected_vehicle_type"] = "motorcycle"
        if child_profile_strong and not adult_blockers_for_child and not road_homologation:
            return category_decision("Pojazdy elektryczne > Dla dzieci i młodzieży", 0.86, "child_vehicle_strong_profile", signals)
        if explicit_child_target and not child_profile_strong:
            signals["rejected_children_reason"] = "adult_or_fullsize_motorcycle_profile"
        if has_l3e:
            return category_decision("Pojazdy elektryczne > Motocykle 125 cc", 0.94, "motorcycle_l3e_without_enduro_profile", signals)
        if has_l1e:
            return category_decision("Pojazdy elektryczne > Motocykle 50 cc", 0.9, "motorcycle_l1e_without_enduro_profile", signals)
        if cc_equivalent == 125:
            return category_decision("Pojazdy elektryczne > Motocykle 125 cc", 0.82, "motorcycle_125_profile", signals)
        if cc_equivalent == 50:
            return category_decision("Pojazdy elektryczne > Motocykle 50 cc", 0.8, "motorcycle_50_profile", signals)
        if adult_weight_signal or large_wheel_signal or explicit_adult_usage:
            return category_decision("Pojazdy elektryczne > Motocykle 125 cc", 0.72, "motorcycle_adult_profile_without_clear_cc", signals)
        return category_decision("", 0.0, "motorcycle_low_confidence", signals)

    if child_profile_strong and not adult_blockers_for_child and detected_product_family == "vehicle":
        signals["detected_vehicle_type"] = "child_vehicle"
        return category_decision("Pojazdy elektryczne > Dla dzieci i młodzieży", 0.82, "child_vehicle_non_enduro_profile", signals)

    # Context-only hints are weak and only used with technical support.
    if has_any(context_text, ["cross", "enduro"]) and (adult_weight_signal or large_wheel_signal or road_homologation):
        signals["detected_vehicle_type"] = "enduro_context_supported"
        return category_decision("Pojazdy elektryczne > Cross / Enduro", 0.62, "context_enduro_with_technical_support", signals)

    return category_decision("", 0.0, "low_confidence", signals)


def classify_vehicle_category(title="", description="", page_text="", manual_category="", source_category="", breadcrumb="", tags="", vehicle_type="", homologation="", weight=None):
    details = classify_vehicle_category_detailed(
        title=title,
        description=description,
        page_text=page_text,
        manual_category=manual_category,
        source_category=source_category,
        breadcrumb=breadcrumb,
        tags=tags,
        vehicle_type=vehicle_type,
        homologation=homologation,
        weight=weight,
    )
    return details.get("category", "") if details.get("confidence", 0.0) >= CATEGORY_CONFIDENCE_THRESHOLD else ""


def score_category(text, category):
    t = ascii_fold(text).lower()
    cat = ascii_fold(category).lower()
    score = 0
    # Hard rules first
    if category == "Pojazdy elektryczne > Microcar":
        if any(k in t for k in ["microcar", "mikrosamochod", "micro samochod", "samochod elektryczny", "czterokolowiec", "l7e", "l6e"]):
            score += 200
    if category == "Skutery elektryczne > Skuter 50 cm³ (L1e)" and "skuter" in t and "l1e" in t:
        score += 180
    if category == "Skutery elektryczne > Skuter 125 cm³ (L3e)" and "skuter" in t and "l3e" in t:
        score += 180
    if category == "Motocykle enduro elektryczne > L1e / do 50 cm³" and "enduro" in t and "l1e" in t:
        score += 180
    if category == "Motocykle enduro elektryczne > L3e / do 125 cm³" and "enduro" in t and "l3e" in t:
        score += 180
    if category == "Motocykle enduro elektryczne > Off-road / bez homologacji (full cross)" and any(k in t for k in ["full cross", "bez homologacji", "off-road"]):
        score += 180

    rule_map = {
        "Pojazdy elektryczne > Dla dzieci i młodzieży": ["dzieci", "mlodziez", "junior", "kids", "youth"],
        "Pojazdy elektryczne > Hulajnogi": ["hulajn"],
        "Pojazdy elektryczne > Quady": ["quad", "atv"],
        "Pojazdy elektryczne > Delivery i Cargo": ["cargo", "delivery", "dostaw", "kuriersk"],
        "Pojazdy elektryczne > Inwalidzkie": ["inwalid", "mobility", "senior", "rehabil"],
        "Pojazdy elektryczne > Golfowe": ["golf"],
        "Pojazdy elektryczne > Cross / Enduro": ["cross", "enduro"],
        "Pojazdy elektryczne > Motocykle 50 cc": ["motocykl", "motor", "50cc", "l1e"],
        "Pojazdy elektryczne > Motocykle 125 cc": ["motocykl", "motor", "125cc", "l3e"],
        "Pojazdy elektryczne > Skutery 50 cc": ["skuter", "50cc"],
        "Pojazdy elektryczne > Skutery 125 cc": ["skuter", "125cc"],
        "Akcesoria > Gogle motocyklowe > Gogle crossowe": ["gogle", "goggle"],
        "Odzież > Kombinezony": ["kombinezon", "odziez"],
        "Modyfikacje > Akumulatory": ["akumulator", "battery", "bateria"],
        "Części i akcesoria > Personalizacja > Okleina Surron Ultra Bee": ["okleina", "ultra bee", "surron"],
        "Części i akcesoria > Personalizacja > Okleina Surron Light Bee": ["okleina", "light bee", "surron"],
        "Części i akcesoria > Personalizacja > Okleina Talaria MX3/MX4": ["okleina", "talaria", "mx3", "mx4"],
        "Części i akcesoria > Personalizacja > Projekt customowy": ["custom", "projekt"],
        "Części i akcesoria > Personalizacja > Siedzenia": ["siedzenie", "seat"],
        "Części i akcesoria > Personalizacja > Koła": ["kolo", "kola", "felga", "wheel"],
        "Części zamienne": ["czesc", "zamienne", "spare"],
    }
    for target, keywords in rule_map.items():
        if category == target:
            for kw in keywords:
                if kw in t:
                    score += 20
    tokens = [tok.strip() for tok in re.split(r"[>/()]", cat) if tok.strip()]
    for token in tokens:
        if len(token) > 3 and token in t:
            score += 6
    score += category.count(">") * 2
    return score


def resolve_category_with_details(title="", description="", page_text="", manual_category="", source_category="", breadcrumb="", tags="", vehicle_type="", homologation="", weight=None):
    details = classify_vehicle_category_detailed(
        title=title,
        description=description,
        page_text=page_text,
        manual_category=manual_category,
        source_category=source_category,
        breadcrumb=breadcrumb,
        tags=tags,
        vehicle_type=vehicle_type,
        homologation=homologation,
        weight=weight,
    )
    category = details.get("category", "")
    confidence = float(details.get("confidence", 0.0))
    if category and confidence >= CATEGORY_CONFIDENCE_THRESHOLD:
        details["method"] = "rules"
        details["score"] = None
        return category, details

    signals = details.get("signals", {}) if isinstance(details.get("signals", {}), dict) else {}
    blocked_categories = set()
    if signals.get("adult_blockers_for_child") or not signals.get("child_profile_strong"):
        blocked_categories.add("Pojazdy elektryczne > Dla dzieci i młodzieży")
        blocked_categories.add("Motocykle enduro elektryczne > Mini cross elektryczny dla dzieci i młodzieży")
    if not signals.get("microcar_strong_signal"):
        blocked_categories.add("Pojazdy elektryczne > Microcar")

    candidate_categories = set(CLASSIFICATION_ALLOWED_CATEGORIES)
    family = safe_str(signals.get("detected_product_family", ""))
    detected_type = safe_str(signals.get("detected_vehicle_type", ""))
    if family == "vehicle":
        candidate_categories &= VEHICLE_CATEGORY_SET
    if family == "part_accessory":
        candidate_categories &= NON_VEHICLE_CATEGORY_SET

    if detected_type == "scooter":
        candidate_categories &= {
            "Skutery elektryczne > Skuter 50 cm³ (L1e)",
            "Skutery elektryczne > Skuter 125 cm³ (L3e)",
            "Pojazdy elektryczne > Skutery 50 cc",
            "Pojazdy elektryczne > Skutery 125 cc",
        }
    elif detected_type == "enduro":
        candidate_categories &= {
            "Motocykle enduro elektryczne > Mini cross elektryczny dla dzieci i młodzieży",
            "Motocykle enduro elektryczne > L1e / do 50 cm³",
            "Motocykle enduro elektryczne > L3e / do 125 cm³",
            "Motocykle enduro elektryczne > Off-road / bez homologacji (full cross)",
            "Pojazdy elektryczne > Cross / Enduro",
        }
    elif detected_type == "motorcycle_general":
        candidate_categories &= {
            "Pojazdy elektryczne > Motocykle 50 cc",
            "Pojazdy elektryczne > Motocykle 125 cc",
            "Pojazdy elektryczne > Dla dzieci i młodzieży",
        }
    elif detected_type in {"quad", "microcar", "standing_scooter", "mobility", "golf", "cargo_delivery"}:
        strict_map = {
            "quad": {"Pojazdy elektryczne > Quady"},
            "microcar": {"Pojazdy elektryczne > Microcar"},
            "standing_scooter": {"Pojazdy elektryczne > Hulajnogi"},
            "mobility": {"Pojazdy elektryczne > Inwalidzkie"},
            "golf": {"Pojazdy elektryczne > Golfowe"},
            "cargo_delivery": {"Pojazdy elektryczne > Delivery i Cargo"},
        }
        candidate_categories &= strict_map.get(detected_type, candidate_categories)
    elif detected_type.startswith("accessory_") or detected_type.startswith("clothing_") or detected_type.startswith("trip_") or detected_type.startswith("wrap_") or detected_type.startswith("eride_") or detected_type.startswith("mod_") or detected_type.startswith("torp_") or detected_type.endswith("_part"):
        candidate_categories &= NON_VEHICLE_CATEGORY_SET

    merged = " ".join([title or "", description or "", page_text or "", tags or "", vehicle_type or "", homologation or ""])
    scores = [
        (score_category(merged, cat), cat)
        for cat in candidate_categories
        if cat not in blocked_categories
    ]
    if not scores:
        details["method"] = "none"
        details["score"] = 0
        details["reason"] = details.get("reason", "low_confidence")
        return "", details
    scores.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
    best_score, best_category = scores[0]
    if best_score >= 56:
        return best_category, {
            "category": best_category,
            "confidence": min(0.8, max(CATEGORY_CONFIDENCE_THRESHOLD, best_score / 100.0)),
            "reason": "score_fallback",
            "signals": details.get("signals", {}),
            "method": "score",
            "score": best_score,
        }
    details["method"] = "none"
    details["score"] = best_score
    return "", details


def guess_category(title="", description="", page_text="", manual_category="", source_category="", breadcrumb="", tags="", vehicle_type="", homologation="", weight=None):
    category, _details = resolve_category_with_details(
        title=title,
        description=description,
        page_text=page_text,
        manual_category=manual_category,
        source_category=source_category,
        breadcrumb=breadcrumb,
        tags=tags,
        vehicle_type=vehicle_type,
        homologation=homologation,
        weight=weight,
    )
    return category


# ==============================
# UI / State
# ==============================
def init_state():
    defaults = {
        "results": [],
        "api_key": "",
        "claude_client": None,
        "bulk_products": [],
        "bulk_selected": {},
        "buying_discount_links": 20.0,
        "buying_discount_bulk": 20.0,
        "buying_discount_manual": 20.0,
        "product_code_counter": 0,
        "debug_mode": False,
        "scraping_errors": [],
        "generation_errors": [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if not st.session_state.api_key:
        try:
            st.session_state.api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
        except Exception:
            st.session_state.api_key = ""
    if st.session_state.api_key and anthropic is not None and st.session_state.claude_client is None:
        st.session_state.claude_client = anthropic.Anthropic(api_key=st.session_state.api_key)


def render_css():
    st.markdown("""
    <style>
    .stApp { background: #0f172a; color: #e5e7eb; }
    .block-container { padding-top: 1.2rem; }
    .hero-card, .soft-card { background: rgba(30,41,59,.8); border:1px solid rgba(148,163,184,.2); border-radius:18px; padding:16px; }
    .pill { display:inline-block; padding:4px 10px; border-radius:999px; background:#1e293b; margin-right:6px; font-size:12px; }
    .muted { color:#94a3b8; }
    .metric-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-top:14px; }
    .metric-box { background:rgba(15,23,42,.65); border:1px solid rgba(148,163,184,.18); border-radius:14px; padding:12px; }
    .metric-label { color:#94a3b8; font-size:12px; }
    .metric-value { font-size:24px; font-weight:800; color:white; }
    </style>
    """, unsafe_allow_html=True)


def render_sidebar():
    with st.sidebar:
        st.markdown("## generator-chatshoper")
        api_key_input = st.text_input("API Key", type="password", value=st.session_state.api_key)
        if api_key_input != st.session_state.api_key:
            st.session_state.api_key = api_key_input.strip()
            if anthropic is not None and st.session_state.api_key:
                st.session_state.claude_client = anthropic.Anthropic(api_key=st.session_state.api_key)
            else:
                st.session_state.claude_client = None
        model = st.selectbox("Model Claude", MODEL_OPTIONS, index=0)
        rewrite_mode = st.checkbox("Tryb rewrite", value=False)
        st.checkbox("Debug mode", key="debug_mode")
        if st.button("Wyczyść wyniki", use_container_width=True):
            st.session_state.results = []
            st.session_state.product_code_counter = 0
        return model, rewrite_mode


def require_client():
    if st.session_state.claude_client is None:
        st.error("Wprowadź poprawny API Key Anthropic w sidebarze.")
        return None
    return st.session_state.claude_client


def next_product_code(name):
    folded = re.sub(r"[^A-Za-z0-9]", "", ascii_fold(name).upper())
    prefix = (folded[:3] if len(folded) >= 3 else folded.ljust(3, "X")) or "PRD"
    counter = int(st.session_state.product_code_counter)
    code = f"{prefix}{counter:04d}"
    st.session_state.product_code_counter = 0 if counter >= 9999 else counter + 1
    return code


def compute_price_buying(price, discount):
    price_float = parse_float(price)
    discount_float = parse_float(discount)
    if price_float is None:
        return ""
    if discount_float is None:
        discount_float = 0.0
    discount_float = max(0.0, min(99.99, float(discount_float)))
    return f"{price_float * (1 - discount_float / 100.0):.2f}"


def dedupe_results(results):
    seen = set()
    deduped = []
    for item in results:
        key = item.get("url") or item.get("name") or item.get("product_code")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


# ==============================
# Shared data schema helpers
# ==============================
SCRAPED_PRODUCT_DEFAULTS = {
    "url": "",
    "title": "",
    "page_text": "",
    "existing_description": "",
    "price": None,
    "weight": None,
    "available": None,
    "images": [],
    "sku": "",
    "breadcrumb": "",
    "source_category": "",
    "tags": "",
    "vehicle_type": "",
    "homologation": "",
    "spec_fields": {},
}

GENERATED_CONTENT_DEFAULTS = {
    "name": "",
    "short_description": "",
    "description": "",
    "seo_title": "",
    "seo_description": "",
    "seo_url": "",
}

RESULT_ITEM_DEFAULTS = {
    "url": "",
    "name": "",
    "product_code": "",
    "category": "",
    "producer": "",
    "weight": None,
    "price": None,
    "available": None,
    "images": [],
    "sku": "",
    "short_description": "",
    "description": "",
    "seo_title": "",
    "seo_description": "",
    "seo_url": "",
    "price_buying": "",
    "buying_discount": 0.0,
    "spec_fields": {},
    "category_reason": "",
    "category_confidence": 0.0,
    "category_method": "",
    "category_score": None,
    "category_signals": {},
}


def normalize_scraped_product(data):
    merged = dict(SCRAPED_PRODUCT_DEFAULTS)
    if isinstance(data, dict):
        for key in merged:
            if key in data:
                merged[key] = data.get(key)
    merged["url"] = safe_str(merged.get("url", ""))
    merged["title"] = safe_str(merged.get("title", ""))
    merged["page_text"] = safe_str(merged.get("page_text", ""))
    merged["existing_description"] = safe_str(merged.get("existing_description", ""))
    merged["price"] = parse_float(merged.get("price"))
    merged["weight"] = parse_float(merged.get("weight"))
    merged["sku"] = safe_str(merged.get("sku", ""))
    merged["breadcrumb"] = safe_str(merged.get("breadcrumb", ""))
    merged["source_category"] = safe_str(merged.get("source_category", ""))
    merged["tags"] = safe_str(merged.get("tags", ""))
    merged["vehicle_type"] = safe_str(merged.get("vehicle_type", ""))
    merged["homologation"] = safe_str(merged.get("homologation", ""))
    merged["spec_fields"] = normalize_spec_fields(merged.get("spec_fields", {}))
    images = merged.get("images") or []
    if not isinstance(images, list):
        images = [images]
    merged["images"] = [safe_str(url) for url in images if safe_str(url)][:MAX_IMAGES]
    return merged


def normalize_generated_content(data):
    merged = dict(GENERATED_CONTENT_DEFAULTS)
    if isinstance(data, dict):
        for key in merged:
            if key in data:
                merged[key] = data.get(key)
    for key in list(merged.keys()):
        merged[key] = safe_str(merged.get(key, ""))
    return merged


def normalize_result_item(data):
    merged = dict(RESULT_ITEM_DEFAULTS)
    if isinstance(data, dict):
        for key in merged:
            if key in data:
                merged[key] = data.get(key)
    merged["url"] = safe_str(merged.get("url", ""))
    merged["name"] = safe_str(merged.get("name", ""))
    merged["product_code"] = safe_str(merged.get("product_code", ""))
    merged["category"] = safe_str(merged.get("category", ""))
    merged["producer"] = safe_str(merged.get("producer", ""))
    merged["weight"] = parse_float(merged.get("weight"))
    merged["price"] = parse_float(merged.get("price"))
    merged["sku"] = safe_str(merged.get("sku", ""))
    merged["short_description"] = safe_str(merged.get("short_description", ""))
    merged["description"] = safe_str(merged.get("description", ""))
    merged["seo_title"] = safe_str(merged.get("seo_title", ""))
    merged["seo_description"] = safe_str(merged.get("seo_description", ""))
    merged["seo_url"] = safe_str(merged.get("seo_url", ""))
    merged["buying_discount"] = parse_float(merged.get("buying_discount")) or 0.0
    merged["spec_fields"] = normalize_spec_fields(merged.get("spec_fields", {}))
    merged["category_reason"] = safe_str(merged.get("category_reason", ""))
    merged["category_method"] = safe_str(merged.get("category_method", ""))
    merged["category_confidence"] = parse_float(merged.get("category_confidence")) or 0.0
    merged["category_score"] = parse_float(merged.get("category_score"))
    merged["category_signals"] = merged.get("category_signals") if isinstance(merged.get("category_signals"), dict) else {}
    if merged.get("price_buying") == "":
        merged["price_buying"] = compute_price_buying(merged.get("price"), merged["buying_discount"])
    else:
        merged["price_buying"] = safe_str(merged.get("price_buying", ""))
    images = merged.get("images") or []
    if not isinstance(images, list):
        images = [images]
    merged["images"] = [safe_str(url) for url in images if safe_str(url)][:MAX_IMAGES]
    return merged


def build_generation_payload(scraped, category="", producer="", keywords="", features="", sku_override=""):
    scraped_data = normalize_scraped_product(scraped)
    spec_fields = normalize_spec_fields(scraped_data.get("spec_fields", {}))
    payload = {
        "name": scraped_data.get("title", ""),
        "url": scraped_data.get("url", ""),
        "category": category,
        "producer": producer,
        "keywords": keywords,
        "features": features,
        "price": scraped_data.get("price", ""),
        "weight": scraped_data.get("weight", ""),
        "sku": sku_override or scraped_data.get("sku", ""),
        "availability": scraped_data.get("available", ""),
        "existing_description": scraped_data.get("existing_description", "")[:3000],
        "page_text": scraped_data.get("page_text", "")[:6000],
        "spec_fields": spec_fields,
        "spec_fields_text": spec_fields_as_text(spec_fields),
        "images": scraped_data.get("images", []),
    }
    return payload


def append_results(new_results):
    if not new_results:
        return
    normalized = [normalize_result_item(item) for item in new_results]
    st.session_state.results = dedupe_results(st.session_state.results + normalized)


def append_error_log(state_key, entry, max_items=200):
    existing = list(st.session_state.get(state_key, []))
    existing.append(entry)
    if len(existing) > max_items:
        existing = existing[-max_items:]
    st.session_state[state_key] = existing


def log_scraping_error(url, exc):
    append_error_log(
        "scraping_errors",
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url": safe_str(url),
            "error": safe_str(exc),
        },
    )


def log_generation_error(url, exc):
    append_error_log(
        "generation_errors",
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url": safe_str(url),
            "error": safe_str(exc),
        },
    )


def result_widget_suffix(item, idx):
    base = safe_str(item.get("product_code") or item.get("url") or item.get("name") or f"item-{idx}")
    folded = re.sub(r"[^a-z0-9]+", "-", ascii_fold(base).lower()).strip("-")
    if not folded:
        folded = f"item-{idx}"
    return f"{folded}-{idx}"


def build_common_result(scraped, generated, manual_category, producer, discount):
    scraped_data = normalize_scraped_product(scraped)
    generated_data = normalize_generated_content(generated)
    spec_fields = normalize_spec_fields(scraped_data.get("spec_fields", {}))
    name = generated_data.get("name") or scraped_data.get("title") or "Produkt"
    description_with_specs = attach_specification_block(generated_data.get("description", ""), spec_fields)
    resolved_category, category_meta = resolve_category_with_details(
        title=name,
        description=" ".join([
            safe_str(generated_data.get("short_description", "")),
            safe_str(description_with_specs),
            safe_str(scraped_data.get("existing_description", "")),
            safe_str(spec_fields_as_text(spec_fields)),
        ]),
        page_text=safe_str(scraped_data.get("page_text", "")),
        manual_category=manual_category,
        source_category=safe_str(scraped_data.get("source_category", "")),
        breadcrumb=safe_str(scraped_data.get("breadcrumb", "")),
        tags=safe_str(scraped_data.get("tags", "")),
        vehicle_type=safe_str(scraped_data.get("vehicle_type", "")),
        homologation=safe_str(scraped_data.get("homologation", "")),
    )
    price = parse_float(scraped_data.get("price"))
    result = {
        "url": safe_str(scraped_data.get("url", "")),
        "name": safe_str(name),
        "product_code": next_product_code(name),
        "category": resolved_category,
        "producer": safe_str(producer or ""),
        "weight": parse_float(scraped_data.get("weight")),
        "price": price,
        "available": scraped_data.get("available"),
        "images": scraped_data.get("images", []),
        "sku": safe_str(scraped_data.get("sku", "")),
        "short_description": safe_str(generated_data.get("short_description", "")),
        "description": safe_str(description_with_specs),
        "seo_title": safe_str(generated_data.get("seo_title", "")),
        "seo_description": safe_str(generated_data.get("seo_description", "")),
        "seo_url": safe_str(generated_data.get("seo_url", slugify(name))),
        "price_buying": compute_price_buying(price, discount),
        "buying_discount": parse_float(discount) or 0.0,
        "spec_fields": spec_fields,
        "category_reason": safe_str(category_meta.get("reason", "")),
        "category_confidence": parse_float(category_meta.get("confidence")) or 0.0,
        "category_method": safe_str(category_meta.get("method", "")),
        "category_score": category_meta.get("score"),
        "category_signals": category_meta.get("signals", {}),
    }
    return normalize_result_item(result)


# ==============================
# CSV / Export
# ==============================
def to_shoper_rows(results):
    rows = []
    for raw_item in results:
        item = normalize_result_item(raw_item)
        price = parse_float(item.get("price"))
        weight = parse_float(item.get("weight"))
        stock_raw = item.get("available")
        stock = 1 if stock_raw is True else 0 if stock_raw is False else 1
        discount = parse_float(item.get("buying_discount")) or 0.0
        price_buying = item.get("price_buying") or compute_price_buying(price, discount)
        row = {
            "product_code": safe_str(item.get("product_code", "")),
            "vat": "23%",
            "unit": "szt.",
            "category": normalize_category(item.get("category", "")),
            "producer": safe_str(item.get("producer", "")),
            "weight": "" if weight is None else f"{weight}".replace(".", ","),
            "active": 1,
            "name": safe_str(item.get("name", "")),
            "short_description": safe_str(item.get("short_description", "")),
            "description": safe_str(item.get("description", "")),
            "price": "" if price is None else f"{price:.2f}".replace(".", ","),
            "stock": stock,
            "seo_title": safe_str(item.get("seo_title", "")),
            "seo_description": safe_str(item.get("seo_description", "")),
            "seo_url": safe_str(item.get("seo_url", "")),
            "price_buying": safe_str(str(price_buying).replace(".", ",")),
        }
        images = item.get("images") or []
        for idx in range(32):
            row[f"images {idx+1}"] = safe_str(images[idx]) if idx < len(images) else ""
        rows.append(row)
    return rows


def export_csv_bytes(results):
    headers = ["product_code", "vat", "unit", "category", "producer", "weight", "active", "name", "short_description", "description", "price", "stock"] + [f"images {i}" for i in range(1, 33)] + ["seo_title", "seo_description", "seo_url", "price_buying"]
    deduped = dedupe_results(results)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, delimiter=";", quoting=csv.QUOTE_ALL, extrasaction="ignore")
    writer.writeheader()
    for row in to_shoper_rows(deduped):
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


# ==============================
# UI Views
# ==============================
def render_intro():
    st.markdown(f"""
    <div class="hero-card">
        <div><span class="pill">generator-chatshoper</span><span class="pill">SEO</span><span class="pill">CSV</span></div>
        <h1 style="margin:8px 0 0 0;">Generator opisów SEO dla Shoper</h1>
        <p class="muted">Szybszy listing, poprawione ceny i poprawne wyliczanie price_buying.</p>
    </div>
    """, unsafe_allow_html=True)


def tab_links(model, rewrite_mode):
    st.subheader("Z linków produktów")
    with st.form("links_form"):
        urls_raw = st.text_area("Wklej URL-e produktów", height=180)
        with st.expander("Ustawienia wspólne", expanded=True):
            c1, c2 = st.columns(2)
            with c1:
                category = st.text_input("Kategoria bazowa / ręczna", key="links_category")
                producer = st.text_input("Producent", key="links_producer")
            with c2:
                keywords = st.text_input("Słowa kluczowe", key="links_keywords")
                features = st.text_area("Dodatkowe cechy", key="links_features", height=90)
            discount = st.number_input("Rabat do wyliczenia price_buying (%)", min_value=0.0, max_value=99.99, step=0.5, key="buying_discount_links")
        submit_links = st.form_submit_button("Generuj dla URL-i", type="primary", use_container_width=True)
    if submit_links:
        urls = parse_urls(urls_raw)
        if not urls:
            st.warning("Dodaj co najmniej jeden poprawny URL.")
            return
        client = require_client()
        if client is None:
            return
        progress = st.progress(0)
        new_results = []
        for idx, url in enumerate(urls, start=1):
            scraped = None
            try:
                scraped = scrape_product_url(url)
            except Exception as exc:
                log_scraping_error(url, exc)
                st.warning(f"Błąd dla {url}: {safe_str(exc)}")
                progress.progress(idx / max(len(urls), 1))
                continue
            payload = build_generation_payload(scraped, category=category, producer=producer, keywords=keywords, features=features)
            try:
                generated = generate_with_claude(client, model, rewrite_mode, payload)
                new_results.append(build_common_result(scraped, generated, category, producer, discount))
            except Exception as exc:
                log_generation_error(url, exc)
                st.warning(f"Błąd generowania dla {url}: {safe_str(exc)}")
            progress.progress(idx / max(len(urls), 1))
        append_results(new_results)
        st.success(f"Gotowe. Dodano {len(new_results)} wyników.")


def tab_manual(model, rewrite_mode):
    st.subheader("Wpisz ręcznie")
    with st.form("manual_form"):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("Nazwa*")
            category = st.text_input("Kategoria")
            producer = st.text_input("Producent")
            sku = st.text_input("SKU")
            price = st.text_input("Cena")
        with c2:
            weight = st.text_input("Waga")
            keywords = st.text_input("Słowa kluczowe")
            discount = st.number_input("Rabat price_buying (%)", min_value=0.0, max_value=99.99, step=0.5, key="buying_discount_manual")
            features = st.text_area("Cechy*", height=120)
        existing_description = st.text_area("Istniejący opis")
        uploaded_image = st.file_uploader("Zdjęcie JPG/PNG (opcjonalne)", type=["jpg", "jpeg", "png"])
        submit = st.form_submit_button("Generuj opis", use_container_width=True)
    if submit:
        client = require_client()
        if client is None:
            return
        scraped = {"url": "manual", "title": name, "sku": sku, "price": parse_float(price), "weight": parse_float(weight), "available": True, "images": [], "page_text": existing_description, "existing_description": existing_description}
        payload = {"name": name, "category": category, "producer": producer, "sku": sku, "price": price, "weight": weight, "keywords": keywords, "features": features, "existing_description": existing_description}
        try:
            generated = generate_with_claude(client, model, rewrite_mode, payload, uploaded_image=uploaded_image)
        except Exception as exc:
            log_generation_error("manual", exc)
            st.error(f"Błąd generowania: {safe_str(exc)}")
            return
        append_results([build_common_result(scraped, generated, category, producer, discount)])
        st.success("Dodano wynik ręczny.")


def tab_bulk(model, rewrite_mode):
    st.subheader("Bulk (kategoria)")
    c1, c2 = st.columns(2)
    with c1:
        listing_url = st.text_input("URL listingu", key="bulk_listing_url")
        category = st.text_input("Kategoria bazowa / ręczna", key="bulk_category")
        keywords = st.text_input("Słowa kluczowe", key="bulk_keywords")
    with c2:
        producer = st.text_input("Producent", key="bulk_producer")
        sku_prefix = st.text_input("Prefix SKU pomocniczy", key="bulk_prefix")
        start_number = st.number_input("Numeracja od", min_value=0, value=1, step=1, key="bulk_start")
    discount = st.number_input("Rabat do wyliczenia price_buying (%)", min_value=0.0, max_value=99.99, step=0.5, key="buying_discount_bulk")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Wczytaj produkty", use_container_width=True):
            try:
                previews, method = scrape_listing_products(listing_url)
                st.session_state.bulk_products = previews
                st.session_state.bulk_selected = {p['url']: True for p in previews}
                st.info(f"Wykryto {len(previews)} produktów. Metoda: {method}")
            except Exception as exc:
                st.error(f"Błąd listingu: {safe_str(exc)}")
    with col2:
        if st.button("Wyczyść listę bulk", use_container_width=True):
            st.session_state.bulk_products = []
            st.session_state.bulk_selected = {}
    if st.session_state.bulk_products:
        s1, s2 = st.columns(2)
        with s1:
            if st.button("Zaznacz wszystkie", use_container_width=True):
                for product in st.session_state.bulk_products:
                    st.session_state.bulk_selected[product['url']] = True
        with s2:
            if st.button("Odznacz wszystkie", use_container_width=True):
                for product in st.session_state.bulk_products:
                    st.session_state.bulk_selected[product['url']] = False
        selected_count = 0
        for product in st.session_state.bulk_products:
            checked = st.checkbox(f"{product.get('title','Bez nazwy')} — {product.get('url')}", value=st.session_state.bulk_selected.get(product['url'], True), key=f"bulk_check_{product['url']}")
            st.session_state.bulk_selected[product['url']] = checked
            selected_count += int(bool(checked))
        st.caption(f"Zaznaczonych: {selected_count}")
        if st.button("Generuj dla zaznaczonych", type="primary", use_container_width=True):
            client = require_client()
            if client is None:
                return
            chosen = [p for p in st.session_state.bulk_products if st.session_state.bulk_selected.get(p['url'], False)]
            progress = st.progress(0)
            new_results = []
            for idx, preview in enumerate(chosen):
                scraped = None
                try:
                    scraped = scrape_product_url(preview['url'])
                except Exception as exc:
                    log_scraping_error(preview.get("url"), exc)
                    st.warning(f"Błąd scrapingu dla {preview.get('url')}: {safe_str(exc)}")
                    progress.progress((idx + 1) / max(len(chosen), 1))
                    continue
                sku = scraped.get("sku") or (f"{sku_prefix}-{str(int(start_number)+idx).zfill(3)}" if sku_prefix else "")
                payload = build_generation_payload(scraped, category=category, producer=producer, keywords=keywords, features="", sku_override=sku)
                try:
                    generated = generate_with_claude(client, model, rewrite_mode, payload)
                    scraped['sku'] = sku
                    new_results.append(build_common_result(scraped, generated, category, producer, discount))
                except Exception as exc:
                    log_generation_error(preview.get("url"), exc)
                    st.warning(f"Błąd generowania dla {preview.get('url')}: {safe_str(exc)}")
                progress.progress((idx + 1) / max(len(chosen), 1))
            append_results(new_results)
            st.success(f"Zakończono generację dla {len(new_results)} produktów.")


def render_results():
    st.markdown("---")
    st.header("Wyniki")
    results = [normalize_result_item(item) for item in dedupe_results(st.session_state.results)]
    st.session_state.results = results
    if not results:
        st.info("Brak wyników do wyświetlenia.")
        return
    top_download_slot = st.empty()
    for idx, item in enumerate(results):
        title = item.get("name") or item.get("url") or f"Produkt {idx+1}"
        header = f"{idx+1}. {title} [{item.get('product_code','')}]"
        suffix = result_widget_suffix(item, idx)
        with st.expander(header):
            st.write(f"URL: {item.get('url','')}")
            item["product_code"] = st.text_input("Product code", value=item.get("product_code",""), key=f"pc_{suffix}")
            category_options = [""] + STORE_CATEGORIES
            current = item.get("category", "")
            item["category"] = st.selectbox("Kategoria", category_options, index=category_options.index(current) if current in category_options else 0, key=f"cat_{suffix}")
            item["producer"] = st.text_input("Producent", value=item.get("producer",""), key=f"prod_{suffix}")
            item["seo_title"] = st.text_input("SEO Title", value=item.get("seo_title",""), key=f"seo_t_{suffix}")
            item["seo_description"] = st.text_area("SEO Description", value=item.get("seo_description",""), key=f"seo_d_{suffix}", height=90)
            item["seo_url"] = st.text_input("SEO URL", value=item.get("seo_url",""), key=f"seo_u_{suffix}")
            item["short_description"] = st.text_area("Krótki opis", value=item.get("short_description",""), key=f"short_{suffix}", height=120)
            item["description"] = st.text_area("Opis HTML", value=item.get("description",""), key=f"desc_{suffix}", height=220)
            col1, col2 = st.columns(2)
            with col1:
                item_price = parse_float(item.get("price"))
                st.text_input("Cena", value="" if item_price is None else f"{item_price:.2f}".replace(".", ","), key=f"price_{suffix}", disabled=True)
                st.text_input("Waga", value=safe_str(item.get("weight","")), key=f"weight_{suffix}", disabled=True)
            with col2:
                item_discount = st.number_input("Rabat price_buying (%)", min_value=0.0, max_value=99.99, step=0.5, value=float(parse_float(item.get("buying_discount")) or 0.0), key=f"disc_{suffix}")
                item["buying_discount"] = item_discount
                item["price_buying"] = compute_price_buying(item.get("price"), item_discount)
                st.text_input("Wyliczony price_buying", value=safe_str(item.get("price_buying","")).replace(".", ","), key=f"pb_{suffix}", disabled=True)
            if st.session_state.debug_mode:
                st.caption(
                    f"Diag kategoryzacji: method={item.get('category_method','')} | "
                    f"reason={item.get('category_reason','')} | "
                    f"confidence={float(parse_float(item.get('category_confidence')) or 0.0):.2f} | "
                    f"score={'' if item.get('category_score') is None else item.get('category_score')}"
                )
                if item.get("category_signals"):
                    st.json(item.get("category_signals"))
            images = item.get("images") or []
            if images and st.checkbox("Pokaż pierwsze zdjęcie", value=False, key=f"show_img_{suffix}"):
                st.image(images[0], caption="Pierwsze zdjęcie produktu", use_container_width=True)
    st.session_state.results = results
    csv_data = export_csv_bytes(results)
    top_download_slot.download_button("Pobierz CSV Shoper", data=csv_data, file_name="generator-chatshoper-export.csv", mime="text/csv", use_container_width=True, key="download_top")
    st.download_button("Pobierz CSV Shoper", data=csv_data, file_name="generator-chatshoper-export.csv", mime="text/csv", use_container_width=True, key="download_bottom")


def render_debug_panel():
    if not st.session_state.debug_mode:
        return
    st.markdown("---")
    st.subheader("Debug / Diagnostyka")
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Błędy scrapingu", len(st.session_state.scraping_errors))
    with col2:
        st.metric("Błędy generowania", len(st.session_state.generation_errors))
    if st.button("Wyczyść logi debug", key="clear_debug_logs"):
        st.session_state.scraping_errors = []
        st.session_state.generation_errors = []
        st.success("Wyczyszczono logi debug.")
    with st.expander("Log błędów scrapingu", expanded=False):
        if not st.session_state.scraping_errors:
            st.caption("Brak błędów scrapingu.")
        for entry in reversed(st.session_state.scraping_errors[-50:]):
            st.write(f"{entry.get('time','')} | {entry.get('url','')} | {entry.get('error','')}")
    with st.expander("Log błędów generowania", expanded=False):
        if not st.session_state.generation_errors:
            st.caption("Brak błędów generowania.")
        for entry in reversed(st.session_state.generation_errors[-50:]):
            st.write(f"{entry.get('time','')} | {entry.get('url','')} | {entry.get('error','')}")


def main():
    st.set_page_config(page_title="generator-chatshoper", layout="wide")
    init_state()
    render_css()
    model, rewrite_mode = render_sidebar()
    render_intro()
    tab1, tab2, tab3 = st.tabs(["Z linków produktów", "Wpisz ręcznie", "Bulk (kategoria)"])
    with tab1:
        tab_links(model, rewrite_mode)
    with tab2:
        tab_manual(model, rewrite_mode)
    with tab3:
        tab_bulk(model, rewrite_mode)
    render_results()
    render_debug_panel()


if __name__ == "__main__":
    main()
