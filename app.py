# -*- coding: utf-8 -*-
import base64
import csv
import html
import io
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
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
CHROME_CANDIDATE_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
OLEK_BROWSER_DEBUG_PORT = 9333
OLEK_BROWSER_VERIFY_TIMEOUT_SECONDS = 90
OLEK_BROWSER_POLL_INTERVAL_SECONDS = 2

CREATE_SYSTEM_PROMPT = (
    "Jestes ekspertem SEO dla polskiego e-commerce, specjalizujesz sie w pojazdach elektrycznych. "
    "Zwracasz TYLKO JSON: {name, short_description (max 280 zn bez HTML), description (HTML 400-700 slow: "
    "intro, h2 cechy, h2 zastosowanie, h2 dlaczego warto, CTA), seo_title (50-60 zn), "
    "seo_description (140-160 zn z CTA), seo_url (slug max 80)}. "
    "WAZNE: zwroc jeden poprawny obiekt JSON i nic poza nim. Bez markdown, bez ```json, bez komentarzy, bez wstepu, bez dopiskow. "
    "Jesli pole description zawiera HTML, nadal musi byc poprawnie zapisane wewnatrz JSON, z prawidlowo escapowanymi cudzyslowami. "
    "Pisz po polsku z polskimi znakami w tresci."
)

REWRITE_SYSTEM_PROMPT = (
    "Jestes ekspertem SEO dla polskiego e-commerce. Przepisujesz istniejace opisy. "
    "Zwracasz TYLKO JSON: {name, short_description, description, seo_title, seo_description, seo_url}. "
    "ZASADY: zachowaj dane techniczne, przepisz wlasnym jezykiem, 400-700 slow. "
    "WAZNE: zwroc jeden poprawny obiekt JSON i nic poza nim. Bez markdown, bez ```json, bez komentarzy, bez wstepu, bez dopiskow. "
    "Jesli pole description zawiera HTML, nadal musi byc poprawnie zapisane wewnatrz JSON, z prawidlowo escapowanymi cudzyslowami. "
    "Pisz po polsku z polskimi znakami w tresci."
)

JSON_REPAIR_SYSTEM_PROMPT = (
    "Naprawiasz odpowiedzi modeli do postaci jednego poprawnego obiektu JSON. "
    "Zwracasz tylko czysty JSON, bez markdown, bez fenced code block, bez komentarzy i bez dodatkowego tekstu. "
    "Zachowaj tresc pol name, short_description, description, seo_title, seo_description, seo_url, ale popraw skladnie JSON. "
    "Jesli description zawiera HTML, zachowaj go jako string JSON z poprawnie escapowanymi cudzyslowami."
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


def normalize_delivery_days(value):
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        days = int(value)
        return f"{days} dni" if days > 0 else ""
    text = normalize_whitespace(value)
    match = re.search(r"(\d+)", text)
    if not match:
        return ""
    days = int(match.group(1))
    return f"{days} dni" if days > 0 else ""


def sanitize_html_for_csv(value):
    text = safe_str(value)
    if not text:
        return ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r">\s+<", "><", text)
    return text.strip()


def html_to_plain_text(value):
    text = safe_str(value)
    if not text:
        return ""
    try:
        return normalize_whitespace(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))
    except Exception:
        return normalize_whitespace(text)


def trim_text_excerpt(text, limit):
    value = normalize_whitespace(text)
    if not value or len(value) <= limit:
        return value
    trimmed = value[: limit + 1].rsplit(" ", 1)[0].strip()
    return trimmed or value[:limit].strip()


def extract_html_title(raw_html):
    text = safe_str(raw_html)
    match = re.search(r"<title>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return normalize_whitespace(html.unescape(match.group(1)))


def make_response_preview(raw_text, limit=2000):
    text = safe_str(raw_text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def sanitize_filename_component(text, fallback="plik"):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_fold(text)).strip("-._")
    return (slug[:80] or fallback).strip("-._") or fallback


def detect_image_extension(url, content_type=""):
    content = safe_str(content_type).split(";", 1)[0].strip().lower()
    if content in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if content == "image/png":
        return ".png"
    path = urlparse(safe_str(url)).path.lower()
    if path.endswith((".jpg", ".jpeg")):
        return ".jpg"
    if path.endswith(".png"):
        return ".png"
    return ""


def download_product_images(image_urls, product_name, source_domain=""):
    urls = []
    seen = set()
    for raw_url in image_urls or []:
        url = safe_str(raw_url)
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    debug = {
        "image_download_requested": bool(urls),
        "downloaded_images_count": 0,
        "downloaded_images_dir": "",
        "downloaded_images_errors": [],
    }
    if not urls:
        return [], "", debug

    root_dir = Path.cwd() / "downloaded-product-images"
    root_dir.mkdir(parents=True, exist_ok=True)
    folder_base = sanitize_filename_component(product_name or "produkt", fallback="produkt")
    domain_prefix = sanitize_filename_component(source_domain or "", fallback="") if source_domain else ""
    folder_name = f"{domain_prefix}-{folder_base}" if domain_prefix else folder_base
    product_dir = root_dir / folder_name
    counter = 2
    while product_dir.exists():
        product_dir = root_dir / f"{folder_name}-{counter}"
        counter += 1
    product_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    session = get_session()
    for idx, url in enumerate(urls, start=1):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            extension = detect_image_extension(url, response.headers.get("Content-Type", ""))
            if not extension:
                debug["downloaded_images_errors"].append(f"unsupported_type:{url}")
                continue
            file_path = product_dir / f"{idx:02d}{extension}"
            file_path.write_bytes(response.content)
            downloaded.append(str(file_path))
        except Exception as exc:
            debug["downloaded_images_errors"].append(f"{url} -> {safe_str(exc)}")

    debug["downloaded_images_count"] = len(downloaded)
    debug["downloaded_images_dir"] = str(product_dir) if downloaded else ""
    return downloaded, (str(product_dir) if downloaded else ""), debug


def enrich_scraped_with_downloaded_images(scraped, should_download=False):
    scraped_data = normalize_scraped_product(scraped)
    if not should_download:
        return scraped_data
    downloaded_images, downloaded_dir, download_debug = download_product_images(
        scraped_data.get("images", []),
        scraped_data.get("title", ""),
        source_domain=scraped_data.get("source_domain", ""),
    )
    scraped_data["downloaded_images"] = downloaded_images
    scraped_data["downloaded_images_dir"] = downloaded_dir
    scrape_debug = dict(scraped_data.get("scrape_debug", {}))
    scrape_debug.update(download_debug)
    scraped_data["scrape_debug"] = scrape_debug
    return scraped_data


def normalize_currency_code(value):
    text = safe_str(value).strip().upper()
    if not text:
        return ""
    folded = ascii_fold(text).upper()
    if "PLN" in folded or "ZL" in folded or "ZŁ" in text:
        return "PLN"
    if "EUR" in folded or "€" in text:
        return "EUR"
    return text if re.fullmatch(r"[A-Z]{3}", text) else ""


def round_price_up_to_tens(value):
    price = parse_float(value)
    if price is None or price <= 0:
        return None
    return float(int(math.ceil(price / 10.0) * 10))


@st.cache_data(show_spinner=False, ttl=43200)
def fetch_exchange_rate(currency_code):
    code = normalize_currency_code(currency_code)
    if not code:
        raise ValueError("Brak kodu waluty do pobrania kursu.")
    if code == "PLN":
        return 1.0
    url = f"https://api.nbp.pl/api/exchangerates/rates/a/{code.lower()}/?format=json"
    response = get_session().get(url, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    rates = data.get("rates") or []
    rate = parse_float((rates[0] or {}).get("mid")) if rates else None
    if rate is None or rate <= 0:
        raise RuntimeError(f"Nieprawidlowy kurs waluty dla {code}.")
    return rate


def detect_currency(soup, page_text, json_ld_items, raw_html="", data_product=None):
    def _from_value(value):
        code = normalize_currency_code(value)
        return code if code in {"PLN", "EUR"} else ""

    if isinstance(data_product, dict):
        for key in ("currency", "currency_code", "currencyCode"):
            code = _from_value(data_product.get(key))
            if code:
                return code

    for data in json_ld_items:
        for item in flatten_json_ld(data):
            if not isinstance(item, dict):
                continue
            for candidate in [item.get("priceCurrency"), item.get("currency"), item.get("currencyCode")]:
                code = _from_value(candidate)
                if code:
                    return code
            offers = item.get("offers")
            if isinstance(offers, dict):
                for candidate in [offers.get("priceCurrency"), offers.get("currency"), offers.get("currencyCode")]:
                    code = _from_value(candidate)
                    if code:
                        return code
            elif isinstance(offers, list):
                for offer in offers:
                    if not isinstance(offer, dict):
                        continue
                    for candidate in [offer.get("priceCurrency"), offer.get("currency"), offer.get("currencyCode")]:
                        code = _from_value(candidate)
                        if code:
                            return code

    for attrs in (
        {"property": "product:price:currency"},
        {"property": "og:price:currency"},
        {"itemprop": "priceCurrency"},
        {"name": "priceCurrency"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            code = _from_value(tag.get("content"))
            if code:
                return code

    product_nodes = []
    for selector in [
        ".summary",
        "main .product",
        ".single-product div.product",
        ".price",
        ".product-price",
        ".current-price",
        ".entry-summary",
    ]:
        product_nodes.extend(soup.select(selector))
    product_text = normalize_whitespace(" ".join(node.get_text(" ", strip=True) for node in product_nodes[:12]))
    focused_blob = " ".join([safe_str(page_text), product_text]).lower()
    if "€" in product_text or " eur" in focused_blob or focused_blob.startswith("eur "):
        return "EUR"
    if "zł" in product_text or " pln" in focused_blob or " zł" in focused_blob:
        return "PLN"

    raw_blob = safe_str(raw_html)
    for pattern in [
        r'"priceCurrency"\s*:\s*"([A-Z]{3})"',
        r'"currencyCode"\s*:\s*"([A-Z]{3})"',
        r'data-currency\s*=\s*"([A-Z]{3})"',
        r'Shopify\.currency\s*=\s*\{[^}]*"active"\s*:\s*"([A-Z]{3})"',
    ]:
        match = re.search(pattern, raw_blob, flags=re.IGNORECASE)
        if match:
            code = _from_value(match.group(1))
            if code:
                return code

    if "€" in raw_blob:
        return "EUR"
    if "zł" in raw_blob.lower():
        return "PLN"
    return ""


def convert_price_to_pln(price, currency_code):
    source_price = parse_float(price)
    source_currency = normalize_currency_code(currency_code)
    debug = {
        "source_currency": source_currency,
        "source_price": source_price,
        "exchange_rate_used": None,
        "converted_price_pln_before_rounding": None,
        "final_price_pln_after_rounding": source_price,
        "conversion_warning": "",
    }
    if source_price is None:
        return None, debug
    if not source_currency:
        debug["conversion_warning"] = "currency_not_detected"
        return source_price, debug
    if source_currency == "PLN":
        debug["exchange_rate_used"] = 1.0
        debug["converted_price_pln_before_rounding"] = source_price
        debug["final_price_pln_after_rounding"] = source_price
        return source_price, debug
    if source_currency != "EUR":
        debug["conversion_warning"] = f"unsupported_currency:{source_currency}"
        return source_price, debug
    try:
        rate = fetch_exchange_rate(source_currency)
        converted = source_price * rate
        rounded = round_price_up_to_tens(converted)
        debug["exchange_rate_used"] = rate
        debug["converted_price_pln_before_rounding"] = converted
        debug["final_price_pln_after_rounding"] = rounded
        return rounded if rounded is not None else source_price, debug
    except Exception as exc:
        debug["conversion_warning"] = f"exchange_rate_fetch_failed:{safe_str(exc)}"
        return source_price, debug


SIZE_OPTION_LABELS = ["size", "rozmiar", "rozmiary", "grösse", "groesse", "taille"]
SIZE_PLACEHOLDER_TERMS = {
    "",
    "-",
    "--",
    "wybierz",
    "wybierz opcje",
    "wybierz opcję",
    "choose an option",
    "choose option",
    "select size",
    "select option",
}
VARIANT_BLOCK_START = "<!-- gc-variant-start -->"
VARIANT_BLOCK_END = "<!-- gc-variant-end -->"


def normalize_variant_options(values):
    if not isinstance(values, list):
        values = [values]
    normalized = []
    seen = set()
    for raw in values:
        value = normalize_whitespace(raw)
        if not value:
            continue
        value = re.sub(r"\s*/\s*", "/", value)
        value = re.sub(r"\s*-\s*", "-", value)
        folded = ascii_fold(value).upper()
        if folded in seen:
            continue
        seen.add(folded)
        normalized.append(value.upper() if re.fullmatch(r"[a-z]{1,5}(?:/[a-z]{1,5})?", value, flags=re.IGNORECASE) else value)
    return normalized[:20]


def is_size_label(text):
    blob = ascii_fold(text).lower()
    return any(label in blob for label in SIZE_OPTION_LABELS)


def looks_like_size_option(value):
    text = normalize_whitespace(value)
    if not text or len(text) > 32:
        return False
    low = ascii_fold(text).lower()
    if low in SIZE_PLACEHOLDER_TERMS:
        return False
    if re.fullmatch(r"(?:one size|onesize|uni|universal|uniwersalny|uniwersalna)", low):
        return True
    if re.fullmatch(r"(?:xxs|xs|s|m|l|xl|xxl|xxxl|xxxxl|4xl|5xl|6xl)(?:/(?:xxs|xs|s|m|l|xl|xxl|xxxl|xxxxl|4xl|5xl|6xl))*", low):
        return True
    if re.fullmatch(r"\d{2,3}(?:\s*[-/]\s*\d{2,3})?(?:\s*(?:cm|eu|us|uk))?", low):
        return True
    if re.fullmatch(r"\d{1,2}[a-z]{0,2}", low):
        return True
    return False


def parse_json_like_value(raw):
    text = html.unescape(safe_str(raw)).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def extract_size_options_from_product_json(product_data):
    if not isinstance(product_data, dict):
        return []
    variants = product_data.get("variants")
    if not isinstance(variants, list) or not variants:
        return []

    option_labels = product_data.get("options") or []
    size_indexes = []
    for idx, label in enumerate(option_labels):
        label_text = label.get("name") if isinstance(label, dict) else label
        if is_size_label(label_text):
            size_indexes.append(idx)

    options = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        if variant.get("available") is False:
            continue
        if size_indexes:
            for idx in size_indexes:
                value = variant.get(f"option{idx+1}")
                if not value and isinstance(variant.get("options"), list) and len(variant["options"]) > idx:
                    value = variant["options"][idx]
                if looks_like_size_option(value):
                    options.append(value)
            continue
        for candidate in [variant.get("public_title"), variant.get("title")]:
            if looks_like_size_option(candidate):
                options.append(candidate)
                break
    normalized = normalize_variant_options(options)
    if size_indexes:
        return normalized
    return normalized if len(normalized) >= 2 else []


def extract_variant_options(soup, raw_html=""):
    detected = []
    debug = {"variant_source": "", "variant_candidates_checked": 0}

    def _extend(values, source):
        nonlocal detected
        normalized = normalize_variant_options(values)
        if not normalized:
            return False
        detected = normalize_variant_options(detected + normalized)
        debug["variant_source"] = debug["variant_source"] or source
        return True

    for attr_name in ["product", "data-product", "data-product-json", "data-product_json", "data-product-json-data"]:
        for node in soup.find_all(attrs={attr_name: True}):
            raw = node.get(attr_name)
            if not raw or '"variants"' not in safe_str(raw):
                continue
            debug["variant_candidates_checked"] += 1
            product_data = parse_json_like_value(raw)
            if _extend(extract_size_options_from_product_json(product_data), f"attr:{attr_name}"):
                return detected, debug

    for script in soup.find_all("script", type="application/json"):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw or '"variants"' not in raw:
            continue
        debug["variant_candidates_checked"] += 1
        product_data = parse_json_like_value(raw)
        if _extend(extract_size_options_from_product_json(product_data), "script:application/json"):
            return detected, debug

    for select in soup.find_all("select"):
        label_blob = " ".join([
            safe_str(select.get("name", "")),
            safe_str(select.get("id", "")),
            safe_str(select.get("aria-label", "")),
            safe_str(select.get("data-index", "")),
        ])
        parent = select.parent
        if parent:
            label_blob = " ".join([label_blob, safe_str(parent.get_text(" ", strip=True))[:120]])
        option_values = []
        for option in select.find_all("option"):
            text = normalize_whitespace(option.get_text(" ", strip=True) or option.get("value", ""))
            if looks_like_size_option(text):
                option_values.append(text)
        if option_values and (is_size_label(label_blob) or len(normalize_variant_options(option_values)) >= 2):
            debug["variant_candidates_checked"] += 1
            if _extend(option_values, "select"):
                return detected, debug

    if raw_html:
        compact = safe_str(raw_html)
        pattern = re.compile(r'"options"\s*:\s*\[\s*"size"\s*\].*?"variants"\s*:\s*\[(.*?)\]', flags=re.IGNORECASE | re.DOTALL)
        match = pattern.search(compact)
        if match:
            options = re.findall(r'"public_title"\s*:\s*"([^"]+)"', match.group(1), flags=re.IGNORECASE)
            if _extend(options, "raw_html.shopify_product"):
                return detected, debug

    return detected, debug


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
    "Materiały",
    "Kompatybilność",
    "Zawartość pudełka",
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
    ("Materiały", ["materialy", "materiały", "material", "materials"]),
    ("Kompatybilność", ["kompatybilnosc", "kompatybilność", "zgodnosc", "zgodność", "compatible with", "compatibility"]),
    ("Zawartość pudełka", ["zawartosc pudelka", "zawartość pudełka", "zawartosc opakowania", "box contents", "in the box"]),
]

SPEC_BLOCK_START = "<!-- gc-spec-start -->"
SPEC_BLOCK_END = "<!-- gc-spec-end -->"
COMBINABLE_SPEC_FIELDS = {"Wymiary", "Materiały", "Kompatybilność", "Zawartość pudełka"}


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


def merge_spec_values(existing, new_value):
    existing_value = normalize_spec_value(existing)
    incoming_value = normalize_spec_value(new_value)
    if not existing_value:
        return incoming_value
    if not incoming_value:
        return existing_value
    existing_parts = [normalize_spec_value(part) for part in existing_value.split(" | ")]
    existing_parts = [part for part in existing_parts if part]
    folded_existing = {normalize_spec_label_key(part) for part in existing_parts}
    folded_new = normalize_spec_label_key(incoming_value)
    if folded_new in folded_existing:
        return existing_value
    return " | ".join(existing_parts + [incoming_value])


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
    if target in COMBINABLE_SPEC_FIELDS:
        spec_fields[target] = merge_spec_values(existing, value)
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


def strip_generated_variant_block(description_html):
    text = safe_str(description_html)
    text = re.sub(
        r"<!--\s*gc-variant-start\s*-->.*?<!--\s*gc-variant-end\s*-->",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text.strip()


def render_variant_options_block(variant_options):
    options = normalize_variant_options(variant_options)
    if not options:
        return ""
    joined = ", ".join(html.escape(option) for option in options)
    return (
        f"{VARIANT_BLOCK_START}\n"
        "<div class=\"gc-variant-note\" style=\"margin-top:20px;padding:16px;border:1px solid #e2e8f0;border-radius:12px;background:#f8fafc;color:#0f172a;\">"
        "<h3 style=\"margin:0 0 8px 0;\">Dostępne warianty</h3>"
        f"<p style=\"margin:0;\">Produkt dostępny w rozmiarach: {joined}</p>"
        "</div>\n"
        f"{VARIANT_BLOCK_END}"
    )


def attach_variant_options_block(description_html, variant_options):
    base_description = strip_generated_variant_block(description_html)
    variant_block = render_variant_options_block(variant_options)
    if not variant_block:
        return base_description
    if not base_description:
        return variant_block
    return f"{base_description}\n\n{variant_block}"


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
class ScrapeDiagnosticError(RuntimeError):
    def __init__(self, message, debug=None):
        super().__init__(message)
        self.debug = debug or {}


@st.cache_resource(show_spinner=False)
def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_html(url):
    response = get_session().get(url, timeout=TIMEOUT)
    source_domain = extract_source_domain(url)
    body_preview = safe_str(response.text[:1200]).lower()
    response_debug = {
        "stage": "fetch_html",
        "source_domain": source_domain,
        "requested_url": safe_str(url),
        "final_url": safe_str(response.url),
        "status_code": response.status_code,
        "response_title": extract_html_title(response.text),
        "response_preview": make_response_preview(response.text),
        "waf_detected": False,
        "waf_vendor": "",
    }
    if response.status_code == 403:
        if source_domain == "www.adidas.pl" or source_domain.endswith(".adidas.pl") or source_domain == "adidas.pl":
            if "waffailoverassets" in body_preview or "automatycznie wykryty alert zwiazany z bezpieczenstwem" in ascii_fold(body_preview):
                response_debug["waf_detected"] = True
                response_debug["waf_vendor"] = "akamai"
                raise ScrapeDiagnosticError(
                    "adidas.pl blokuje bezpośrednie requesty tego scrapera przez Akamai/WAF (403). "
                    "Struktura listingu jest czytelna, ale do pobrania tego sklepu potrzebny jest fallback browser-based.",
                    debug=response_debug,
                )
    try:
        response.raise_for_status()
    except Exception as exc:
        raise ScrapeDiagnosticError(
            f"Błąd HTTP podczas pobierania strony: {safe_str(exc)}",
            debug=response_debug,
        ) from exc
    return response.url, response.text


def detect_waf_vendor_from_html(raw_html):
    raw_text = safe_str(raw_html)
    blob = ascii_fold(raw_text).lower()
    title = ascii_fold(extract_html_title(raw_text)).lower()
    if "waffailoverassets" in blob or "automatycznie wykryty alert zwiazany z bezpieczenstwem" in blob:
        return "akamai"
    cloudflare_challenge_markers = [
        "just a moment",
        "attention required!",
        "cf-browser-verification",
        "challenge-error-text",
        "_cf_chl_opt",
        "/cdn-cgi/challenge-platform/",
        "enable javascript and cookies to continue",
        "cf-chl-",
    ]
    if title in {"just a moment...", "attention required! | cloudflare"}:
        return "cloudflare"
    if any(marker in blob for marker in cloudflare_challenge_markers):
        return "cloudflare"
    return ""


def find_chrome_binary():
    for candidate in CHROME_CANDIDATE_PATHS:
        if Path(candidate).exists():
            return candidate
    for name in ["google-chrome", "chromium", "chromium-browser", "chrome"]:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return ""


def get_olek_browser_profile_dir():
    base_dir = Path.cwd() / ".browser-sessions"
    profile_dir = base_dir / "olek-chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def get_chrome_debug_endpoint(port=OLEK_BROWSER_DEBUG_PORT):
    endpoint = f"http://127.0.0.1:{int(port)}/json/version"
    try:
        response = requests.get(endpoint, timeout=1.5)
        response.raise_for_status()
        data = response.json()
        websocket_url = safe_str(data.get("webSocketDebuggerUrl", ""))
        browser_version = safe_str(data.get("Browser", ""))
        if websocket_url:
            return {
                "endpoint_url": endpoint,
                "websocket_url": websocket_url,
                "browser_version": browser_version,
            }
    except Exception:
        return {}
    return {}


def fetch_html_with_browser_cookies(url, cookies, user_agent=""):
    session = requests.Session()
    session.headers.update(HEADERS)
    if user_agent:
        session.headers["User-Agent"] = safe_str(user_agent)
    parsed = urlparse(safe_str(url))
    for cookie in cookies or []:
        name = safe_str(cookie.get("name", ""))
        value = safe_str(cookie.get("value", ""))
        if not name:
            continue
        domain = safe_str(cookie.get("domain", "")).lstrip(".") or parsed.hostname or ""
        path = safe_str(cookie.get("path", "")) or "/"
        session.cookies.set(name, value, domain=domain, path=path)
    response = session.get(safe_str(url), timeout=TIMEOUT)
    response.raise_for_status()
    return safe_str(response.url), safe_str(response.text)


def launch_olek_browser_session(url, port=OLEK_BROWSER_DEBUG_PORT):
    chrome_path = find_chrome_binary()
    append_olek_trace("launch_session_start", url=url, port=port)
    if not chrome_path:
        append_olek_trace("launch_session_no_browser", url=url, port=port)
        raise ScrapeDiagnosticError(
            "Nie znaleziono lokalnej przeglądarki Chrome/Chromium do sesji Cloudflare.",
            debug={
                "stage": "launch_olek_browser_session",
                "requested_url": safe_str(url),
                "browser_path_found": False,
            },
        )

    existing_endpoint = get_chrome_debug_endpoint(port)
    if existing_endpoint:
        append_olek_trace(
            "launch_session_reuse_existing_debugger",
            url=url,
            port=port,
            endpoint_url=existing_endpoint.get("endpoint_url", ""),
            browser_version=existing_endpoint.get("browser_version", ""),
        )
        return {
            "launched": False,
            "browser_path": chrome_path,
            "profile_dir": str(get_olek_browser_profile_dir()),
            "debug_port": int(port),
            "endpoint_url": existing_endpoint.get("endpoint_url", ""),
            "browser_version": existing_endpoint.get("browser_version", ""),
        }

    profile_dir = get_olek_browser_profile_dir()
    cmd = [
        chrome_path,
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        safe_str(url),
    ]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        append_olek_trace("launch_session_popen_error", url=url, port=port, error=safe_str(exc))
        raise ScrapeDiagnosticError(
            f"Nie udało się uruchomić lokalnej sesji Chrome dla Olek: {safe_str(exc)}",
            debug={
                "stage": "launch_olek_browser_session",
                "requested_url": safe_str(url),
                "browser_path": chrome_path,
                "browser_path_found": True,
                "profile_dir": str(profile_dir),
                "debug_port": int(port),
            },
        ) from exc

    deadline = time.time() + 15
    endpoint_data = {}
    while time.time() < deadline:
        endpoint_data = get_chrome_debug_endpoint(port)
        if endpoint_data:
            break
        time.sleep(0.5)
    append_olek_trace(
        "launch_session_ready",
        url=url,
        port=port,
        endpoint_url=endpoint_data.get("endpoint_url", ""),
        browser_version=endpoint_data.get("browser_version", ""),
        profile_dir=str(profile_dir),
    )

    return {
        "launched": True,
        "browser_path": chrome_path,
        "profile_dir": str(profile_dir),
        "debug_port": int(port),
        "endpoint_url": endpoint_data.get("endpoint_url", ""),
        "browser_version": endpoint_data.get("browser_version", ""),
    }


def fetch_html_via_persistent_browser_session(url, verify_timeout_seconds=OLEK_BROWSER_VERIFY_TIMEOUT_SECONDS):
    chrome_path = find_chrome_binary()
    append_olek_trace("persistent_fetch_start", url=url, verify_timeout_seconds=verify_timeout_seconds)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise ScrapeDiagnosticError(
            "Brakuje biblioteki Playwright do sesji persistent browser dla Cloudflare.",
            debug={
                "stage": "fetch_html_persistent_browser",
                "requested_url": safe_str(url),
                "browser_path": chrome_path,
                "browser_path_found": bool(chrome_path),
                "playwright_available": False,
                "playwright_import_error": safe_str(exc),
            },
        ) from exc

    launch_debug = launch_olek_browser_session(url, port=OLEK_BROWSER_DEBUG_PORT)
    endpoint_data = get_chrome_debug_endpoint(OLEK_BROWSER_DEBUG_PORT)
    if not endpoint_data:
        append_olek_trace("persistent_fetch_no_debug_endpoint", url=url, launch_debug=launch_debug)
        raise ScrapeDiagnosticError(
            "Nie udało się nawiązać połączenia z lokalną sesją Chrome dla Olek.",
            debug={
                "stage": "fetch_html_persistent_browser",
                "requested_url": safe_str(url),
                "browser_path": chrome_path,
                "browser_path_found": bool(chrome_path),
                "launch_debug": launch_debug,
                "debug_port": int(OLEK_BROWSER_DEBUG_PORT),
            },
        )

    browser_debug = {
        "stage": "fetch_html_persistent_browser",
        "requested_url": safe_str(url),
        "browser_path": chrome_path,
        "browser_path_found": bool(chrome_path),
        "launch_debug": launch_debug,
        "debug_port": int(OLEK_BROWSER_DEBUG_PORT),
        "profile_dir": str(get_olek_browser_profile_dir()),
        "endpoint_url": endpoint_data.get("endpoint_url", ""),
        "browser_version": endpoint_data.get("browser_version", ""),
        "verification_timeout_seconds": int(verify_timeout_seconds),
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{int(OLEK_BROWSER_DEBUG_PORT)}")
        except Exception as exc:
            append_olek_trace("persistent_fetch_connect_over_cdp_error", url=url, error=safe_str(exc))
            browser_debug["connect_over_cdp_error"] = safe_str(exc)
            raise ScrapeDiagnosticError(
                f"Nie udało się podłączyć do lokalnej sesji Chrome dla Olek: {safe_str(exc)}",
                debug=browser_debug,
            ) from exc

        try:
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
            else:
                context = browser.new_context()
            browser_debug["existing_pages"] = [safe_str(existing_page.url) for existing_page in context.pages[:10]]
            target_host = urlparse(safe_str(url)).netloc.lower()
            page = None
            matching_pages = []
            for existing_page in context.pages:
                page_url = safe_str(existing_page.url)
                page_host = urlparse(page_url).netloc.lower()
                if page_host == target_host:
                    matching_pages.append(existing_page)
            if matching_pages:
                page = matching_pages[-1]
                browser_debug["reused_existing_page"] = True
            else:
                page = context.new_page()
                browser_debug["reused_existing_page"] = False
            append_olek_trace(
                "persistent_fetch_page_selected",
                url=url,
                existing_pages=browser_debug.get("existing_pages", []),
                reused_existing_page=browser_debug.get("reused_existing_page"),
            )
            page.bring_to_front()
            current_url = safe_str(page.url or "")
            current_host = urlparse(current_url).netloc.lower()
            should_navigate = current_host != target_host or not current_url
            browser_debug["selected_page_url"] = current_url
            browser_debug["should_navigate_initially"] = should_navigate
            if should_navigate:
                append_olek_trace("persistent_fetch_initial_goto", url=url, selected_page_url=current_url)
                page.goto(safe_str(url), wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                browser_debug["networkidle_timeout"] = True

            deadline = time.time() + max(10, int(verify_timeout_seconds))
            last_html = ""
            last_url = safe_str(page.url or url)
            reloaded_after_clearance = False
            browser_user_agent = ""
            while time.time() < deadline:
                try:
                    last_url = safe_str(page.url or url)
                    last_html = safe_str(page.content())
                except Exception:
                    last_html = ""
                if not browser_user_agent:
                    try:
                        browser_user_agent = safe_str(page.evaluate("() => navigator.userAgent"))
                    except Exception:
                        browser_user_agent = ""
                waf_vendor = detect_waf_vendor_from_html(last_html)
                try:
                    cookies = context.cookies([safe_str(url)])
                except Exception:
                    cookies = []
                cookie_names = [safe_str(cookie.get("name", "")) for cookie in cookies if safe_str(cookie.get("name", ""))]
                has_cf_clearance = "cf_clearance" in cookie_names
                browser_debug["response_title"] = normalize_whitespace(page.title() or "")
                browser_debug["final_url"] = last_url
                browser_debug["response_preview"] = make_response_preview(last_html)
                browser_debug["waf_detected"] = bool(waf_vendor)
                browser_debug["waf_vendor"] = waf_vendor
                browser_debug["cookie_names"] = cookie_names[:20]
                browser_debug["has_cf_clearance"] = has_cf_clearance
                browser_debug["reloaded_after_clearance"] = reloaded_after_clearance
                browser_debug["browser_user_agent"] = browser_user_agent
                append_olek_trace(
                    "persistent_fetch_poll",
                    url=url,
                    final_url=last_url,
                    waf_vendor=waf_vendor,
                    has_cf_clearance=has_cf_clearance,
                    reloaded_after_clearance=reloaded_after_clearance,
                    cookie_names=cookie_names[:20],
                    response_title=browser_debug.get("response_title", ""),
                )
                if last_html.strip() and not waf_vendor:
                    append_olek_trace("persistent_fetch_success_dom", url=url, final_url=last_url)
                    try:
                        page.close()
                    except Exception:
                        pass
                    return last_url, last_html
                if has_cf_clearance:
                    try:
                        requests_url, requests_html = fetch_html_with_browser_cookies(
                            safe_str(url),
                            cookies,
                            user_agent=browser_user_agent,
                        )
                        requests_waf_vendor = detect_waf_vendor_from_html(requests_html)
                        browser_debug["requests_with_cookies_final_url"] = requests_url
                        browser_debug["requests_with_cookies_preview"] = make_response_preview(requests_html)
                        browser_debug["requests_with_cookies_waf_vendor"] = requests_waf_vendor
                        append_olek_trace(
                            "persistent_fetch_requests_with_cookies",
                            url=url,
                            final_url=requests_url,
                            waf_vendor=requests_waf_vendor,
                        )
                        if requests_html.strip() and not requests_waf_vendor:
                            append_olek_trace("persistent_fetch_success_requests_with_cookies", url=url, final_url=requests_url)
                            try:
                                page.close()
                            except Exception:
                                pass
                            return requests_url, requests_html
                    except Exception as exc:
                        append_olek_trace("persistent_fetch_requests_with_cookies_error", url=url, error=safe_str(exc))
                        browser_debug["requests_with_cookies_error"] = safe_str(exc)
                if waf_vendor == "cloudflare" and has_cf_clearance and not reloaded_after_clearance:
                    try:
                        append_olek_trace("persistent_fetch_reload_after_clearance", url=url, current_page_url=safe_str(page.url or ""))
                        if safe_str(page.url or "") != safe_str(url):
                            page.goto(safe_str(url), wait_until="domcontentloaded", timeout=30000)
                        else:
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=10000)
                        except PlaywrightTimeoutError:
                            browser_debug["networkidle_timeout_after_reload"] = True
                        reloaded_after_clearance = True
                        continue
                    except Exception as exc:
                        append_olek_trace("persistent_fetch_reload_after_clearance_error", url=url, error=safe_str(exc))
                        browser_debug["reload_after_clearance_error"] = safe_str(exc)
                time.sleep(OLEK_BROWSER_POLL_INTERVAL_SECONDS)

            append_olek_trace(
                "persistent_fetch_timeout",
                url=url,
                final_url=browser_debug.get("final_url", ""),
                has_cf_clearance=browser_debug.get("has_cf_clearance"),
                waf_vendor=browser_debug.get("waf_vendor", ""),
            )
            raise ScrapeDiagnosticError(
                "Cloudflare nadal blokuje dostęp. Otwarta została dedykowana sesja Chrome dla Olek. "
                "Zakończ challenge w tej samej przeglądarce i ponów próbę.",
                debug=browser_debug,
            )
        finally:
            pass


def fetch_html_with_playwright(url, wait_timeout_ms=25000):
    chrome_path = find_chrome_binary()
    if not chrome_path:
        raise ScrapeDiagnosticError(
            "Nie znaleziono lokalnej przeglądarki Chrome/Chromium do fallbacku Playwright.",
            debug={
                "stage": "fetch_html_playwright",
                "requested_url": safe_str(url),
                "browser_path_found": False,
            },
        )

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise ScrapeDiagnosticError(
            "Brakuje biblioteki Playwright do mocniejszego fallbacku Cloudflare.",
            debug={
                "stage": "fetch_html_playwright",
                "requested_url": safe_str(url),
                "browser_path": chrome_path,
                "browser_path_found": True,
                "playwright_available": False,
                "playwright_import_error": safe_str(exc),
            },
        ) from exc

    with tempfile.TemporaryDirectory(prefix="generator-chatshoper-olek-pw-") as tmp_dir:
        user_data_dir = str(Path(tmp_dir) / "playwright-profile")
        browser_debug = {
            "stage": "fetch_html_playwright",
            "requested_url": safe_str(url),
            "browser_path": chrome_path,
            "browser_path_found": True,
            "playwright_available": True,
            "playwright_headless": False,
            "playwright_wait_timeout_ms": int(wait_timeout_ms),
        }
        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    executable_path=chrome_path,
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                    viewport={"width": 1440, "height": 2200},
                    user_agent=HEADERS.get("User-Agent", ""),
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
                    Object.defineProperty(navigator, 'language', {get: () => 'pl-PL'});
                    Object.defineProperty(navigator, 'languages', {get: () => ['pl-PL', 'pl', 'en-US', 'en']});
                    window.chrome = window.chrome || { runtime: {} };
                    """
                )
                page.goto(safe_str(url), wait_until="domcontentloaded", timeout=wait_timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except PlaywrightTimeoutError:
                    browser_debug["networkidle_timeout"] = True
                try:
                    page.wait_for_timeout(12000)
                except Exception:
                    pass
                final_url = safe_str(page.url or url)
                html_text = safe_str(page.content())
                browser_debug["final_url"] = final_url
                browser_debug["response_title"] = normalize_whitespace(page.title() or "")
                browser_debug["response_preview"] = make_response_preview(html_text)
                waf_vendor = detect_waf_vendor_from_html(html_text)
                if waf_vendor == "cloudflare":
                    try:
                        page.wait_for_timeout(12000)
                    except Exception:
                        pass
                    final_url = safe_str(page.url or url)
                    html_text = safe_str(page.content())
                    browser_debug["final_url"] = final_url
                    browser_debug["response_title"] = normalize_whitespace(page.title() or "")
                    browser_debug["response_preview"] = make_response_preview(html_text)
                    waf_vendor = detect_waf_vendor_from_html(html_text)
                browser_debug["waf_detected"] = bool(waf_vendor)
                browser_debug["waf_vendor"] = waf_vendor
                context.close()
        except Exception as exc:
            browser_debug["playwright_exception"] = safe_str(exc)
            raise ScrapeDiagnosticError(
                f"Playwright fallback nie uruchomił się poprawnie: {safe_str(exc)}",
                debug=browser_debug,
            ) from exc

    if not html_text.strip():
        raise ScrapeDiagnosticError(
            "Playwright fallback nie zwrócił poprawnego DOM.",
            debug=browser_debug,
        )
    if browser_debug.get("waf_vendor"):
        raise ScrapeDiagnosticError(
            f"Playwright fallback nadal widzi blokadę {browser_debug.get('waf_vendor')}.",
            debug=browser_debug,
        )
    return final_url, html_text


def fetch_html_with_local_chrome(url, virtual_time_budget_ms=12000):
    chrome_path = find_chrome_binary()
    if not chrome_path:
        raise ScrapeDiagnosticError(
            "Nie znaleziono lokalnej przeglądarki Chrome/Chromium do browser fallback.",
            debug={
                "stage": "fetch_html_browser",
                "requested_url": safe_str(url),
                "browser_path_found": False,
            },
        )

    with tempfile.TemporaryDirectory(prefix="generator-chatshoper-olek-") as tmp_dir:
        user_data_dir = str(Path(tmp_dir) / "chrome-profile")
        timeout_seconds = max(20, int(math.ceil(virtual_time_budget_ms / 1000.0)) + 10)
        command_variants = [
            [
                chrome_path,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                f"--user-data-dir={user_data_dir}",
                f"--virtual-time-budget={int(virtual_time_budget_ms)}",
                "--dump-dom",
                safe_str(url),
            ],
            [
                chrome_path,
                "--headless",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--user-data-dir={user_data_dir}",
                f"--virtual-time-budget={int(virtual_time_budget_ms)}",
                "--dump-dom",
                safe_str(url),
            ],
            [
                chrome_path,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--virtual-time-budget={int(virtual_time_budget_ms)}",
                "--dump-dom",
                safe_str(url),
            ],
        ]

        attempt_debug = []
        for attempt_idx, cmd in enumerate(command_variants, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except Exception as exc:
                attempt_debug.append({
                    "attempt": attempt_idx,
                    "command_preview": " ".join(cmd[1:6]),
                    "exception": safe_str(exc),
                })
                continue

            html_text = safe_str(result.stdout)
            stderr_text = safe_str(result.stderr)
            waf_vendor = detect_waf_vendor_from_html(html_text)
            attempt_debug.append({
                "attempt": attempt_idx,
                "command_preview": " ".join(cmd[1:6]),
                "return_code": result.returncode,
                "response_title": extract_html_title(html_text),
                "response_preview": make_response_preview(html_text, limit=500),
                "stderr_preview": make_response_preview(stderr_text, limit=500),
                "waf_vendor": waf_vendor,
            })
            if result.returncode == 0 and html_text.strip() and not waf_vendor:
                return safe_str(url), html_text
            if result.returncode == 0 and html_text.strip() and waf_vendor:
                raise ScrapeDiagnosticError(
                    f"Browser fallback nadal widzi blokadę {waf_vendor}.",
                    debug={
                        "stage": "fetch_html_browser",
                        "requested_url": safe_str(url),
                        "browser_path": chrome_path,
                        "browser_path_found": True,
                        "browser_return_code": result.returncode,
                        "browser_attempts": attempt_debug,
                        "browser_stderr_preview": make_response_preview(stderr_text),
                        "response_preview": make_response_preview(html_text),
                        "response_title": extract_html_title(html_text),
                        "waf_detected": True,
                        "waf_vendor": waf_vendor,
                    },
                )

    raise ScrapeDiagnosticError(
        "Browser fallback nie zwrócił poprawnego DOM.",
        debug={
            "stage": "fetch_html_browser",
            "requested_url": safe_str(url),
            "browser_path": chrome_path,
            "browser_path_found": True,
            "browser_attempts": attempt_debug,
            "waf_detected": False,
            "waf_vendor": "",
        },
    )


def fetch_olek_html(url):
    final_url, html_text, _ = fetch_olek_html_details(url)
    return final_url, html_text


def fetch_olek_html_details(url):
    try:
        final_url, html_text = fetch_html(url)
        return final_url, html_text, "requests"
    except ScrapeDiagnosticError as exc:
        debug = getattr(exc, "debug", {}) if exc is not None else {}
        waf_vendor = safe_str(debug.get("waf_vendor", "")) or detect_waf_vendor_from_html(debug.get("response_preview", ""))
        status_code = safe_str(debug.get("status_code", ""))
        if waf_vendor == "cloudflare" or status_code == "403":
            persistent_error = None
            try:
                final_url, html_text = fetch_html_via_persistent_browser_session(url)
                return final_url, html_text, "browser_session_reuse"
            except ScrapeDiagnosticError as persistent_exc:
                persistent_error = persistent_exc
            chrome_error = None
            try:
                final_url, html_text = fetch_html_with_local_chrome(url)
                return final_url, html_text, "browser_fallback_chrome"
            except ScrapeDiagnosticError as chrome_exc:
                chrome_error = chrome_exc
            try:
                final_url, html_text = fetch_html_with_playwright(url)
                return final_url, html_text, "browser_fallback_playwright"
            except ScrapeDiagnosticError as pw_exc:
                combined_debug = {
                    "stage": "fetch_olek_html",
                    "requested_url": safe_str(url),
                    "initial_fetch_debug": debug,
                    "persistent_browser_debug": getattr(persistent_error, "debug", {}) if persistent_error else {},
                    "chrome_fallback_debug": getattr(chrome_error, "debug", {}) if chrome_error else {},
                    "playwright_fallback_debug": getattr(pw_exc, "debug", {}),
                }
                raise ScrapeDiagnosticError(
                    f"{safe_str(persistent_error) or 'Sesja persistent browser nie powiodła się.'} | "
                    f"{safe_str(chrome_error) or 'Chrome fallback nie powiódł się.'} | {safe_str(pw_exc)}",
                    debug=combined_debug,
                ) from pw_exc
        raise


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


def extract_source_domain(url):
    return urlparse(safe_str(url)).netloc.lower()


def is_sansa_url(url):
    raw = safe_str(url).strip()
    host = extract_source_domain(raw) if "://" in raw else raw.strip("/").lower()
    return host == "sansaeurope.pl" or host.endswith(".sansaeurope.pl")


def is_adidas_url(url):
    raw = safe_str(url).strip()
    host = extract_source_domain(raw) if "://" in raw else raw.strip("/").lower()
    return host == "adidas.pl" or host == "www.adidas.pl" or host.endswith(".adidas.pl")


def is_adidas_product_url(url):
    parsed = urlparse(safe_str(url))
    return is_adidas_url(url) and re.search(r"/[^/]+/[A-Z0-9]{5,}\.html$", parsed.path) is not None


def is_olek_url(url):
    raw = safe_str(url).strip()
    host = extract_source_domain(raw) if "://" in raw else raw.strip("/").lower()
    return host == "shop.olekmotocykle.com" or host.endswith(".olekmotocykle.com")


def is_olek_listing_url(url):
    parsed = urlparse(safe_str(url))
    return is_olek_url(url) and parsed.path.lower().startswith("/produkty") and re.search(r",2(?:,\d+)?$", parsed.path) is not None


def is_olek_product_url(url):
    parsed = urlparse(safe_str(url))
    return is_olek_url(url) and re.search(r",3,\d+,\d+$", parsed.path) is not None


def parse_olek_reference_text(text):
    blob = normalize_whitespace(text)
    sku = ""
    ean = ""
    brand = ""
    sku_match = re.search(r"(?:kod produktu|symbol|sku)\s*[:\-]?\s*([A-Za-z0-9._/\-]+)", blob, flags=re.IGNORECASE)
    ean_match = re.search(r"(?:ean|ean13)\s*[:\-]?\s*([0-9A-Za-z._/\-]+)", blob, flags=re.IGNORECASE)
    brand_match = re.search(r"(?:producent|marka)\s*[:\-]?\s*([A-Za-z0-9ĄĆĘŁŃÓŚŹŻąćęłńóśźż .&/\-]{2,80})", blob, flags=re.IGNORECASE)
    if sku_match:
        sku = normalize_whitespace(sku_match.group(1))
    if ean_match:
        ean = normalize_whitespace(ean_match.group(1))
    if brand_match:
        brand = normalize_whitespace(brand_match.group(1))
    return sku, ean, brand


def extract_olek_title_from_anchor(anchor):
    if anchor is None:
        return ""
    candidates = [
        normalize_whitespace(anchor.get("title", "")),
        normalize_whitespace(anchor.get_text(" ", strip=True)),
    ]
    parent = anchor.parent
    for _ in range(3):
        if parent is None or getattr(parent, "name", None) is None:
            break
        for selector in [
            "h1", "h2", "h3", "h4",
            ".name", ".product-name", ".product_title", ".product-title",
            ".title", ".headline",
        ]:
            node = parent.select_one(selector)
            if node:
                candidates.append(normalize_whitespace(node.get_text(" ", strip=True)))
        parent = parent.parent
    for candidate in candidates:
        if candidate and len(candidate) >= 4:
            return candidate
    return ""


def scrape_olek_listing(url):
    final_url, html_doc, fetch_method = fetch_olek_html_details(url)
    soup = BeautifulSoup(html_doc, "html.parser")
    preview = []
    seen = set()
    candidate_anchor_count = 0
    selector_hits = {}

    selectors = [
        "main a[href]",
        ".products a[href]",
        ".product a[href]",
        "a[href]",
    ]

    for selector in selectors:
        anchors = soup.select(selector)
        selector_hits[selector] = len(anchors)
        for anchor in anchors:
            href = anchor.get("href")
            if not href:
                continue
            absolute = urljoin(final_url, href)
            if not is_olek_product_url(absolute):
                continue
            candidate_anchor_count += 1
            full = canonicalize_product_url(absolute, final_url)
            if full in seen:
                continue
            seen.add(full)
            title = extract_olek_title_from_anchor(anchor) or full
            preview.append({
                "url": full,
                "title": title,
                "source_domain": extract_source_domain(full),
            })
            if len(preview) >= MAX_LISTING_PRODUCTS:
                break
        if len(preview) >= MAX_LISTING_PRODUCTS:
            break

    if preview:
        return preview, f"Olek listing ({fetch_method})"

    raise ScrapeDiagnosticError(
        "Nie znaleziono kart produktowych na listingu Olek Motocykle.",
        debug={
            "stage": "scrape_olek_listing",
            "source_domain": extract_source_domain(final_url),
            "requested_url": safe_str(url),
            "final_url": safe_str(final_url),
            "fetch_method": fetch_method,
            "selectors_tried": selectors,
            "selector_hits": selector_hits,
            "candidate_anchor_count": candidate_anchor_count,
            "response_title": extract_html_title(html_doc),
            "response_preview": make_response_preview(html_doc),
            "waf_detected": False,
            "waf_vendor": "",
        },
    )


def scrape_olek_product(url):
    final_url, html_doc, fetch_method = fetch_olek_html_details(url)
    soup = BeautifulSoup(html_doc, "html.parser")
    page_text = extract_product_main_text(soup)
    json_ld_items = extract_json_ld(soup)

    title = ""
    for selector in ["h1", ".product-name", ".product-title", ".name"]:
        node = soup.select_one(selector)
        if node:
            title = normalize_whitespace(node.get_text(" ", strip=True))
            if title:
                break
    if not title:
        title = normalize_whitespace(soup.title.get_text(" ", strip=True) if soup.title else "")

    reference_nodes = []
    for selector in [
        ".product-code", ".product-index", ".product-symbol", ".product-details",
        ".product-info", ".product-parameters", ".opis", ".description", ".details",
    ]:
        for node in soup.select(selector):
            text = normalize_whitespace(node.get_text(" ", strip=True))
            if text and len(text) <= 600:
                reference_nodes.append(text)
    reference_blob = " | ".join(dict.fromkeys(reference_nodes))
    sku, ean, brand = parse_olek_reference_text(reference_blob)

    breadcrumb = ""
    breadcrumb_nodes = soup.select(".breadcrumb a, .breadcrumbs a, nav.breadcrumb a")
    if breadcrumb_nodes:
        breadcrumb = " / ".join(
            normalize_whitespace(node.get_text(" ", strip=True))
            for node in breadcrumb_nodes
            if normalize_whitespace(node.get_text(" ", strip=True))
        )
    if not breadcrumb:
        breadcrumb = extract_breadcrumb_category(soup)

    source_category = ""
    if breadcrumb:
        breadcrumb_parts = [part.strip() for part in breadcrumb.split("/") if normalize_whitespace(part)]
        if breadcrumb_parts:
            source_category = normalize_whitespace(breadcrumb_parts[-1])

    existing_description = ""
    description_selectors = [
        ".product-description", ".description", ".opis", ".product-tabs",
        ".product-content", ".content", ".details", ".long-description",
    ]
    for selector in description_selectors:
        node = soup.select_one(selector)
        if node and not is_blocked_text_node(node):
            text = normalize_whitespace(node.get_text(" ", strip=True))
            if len(text) > 80:
                existing_description = text[:5000]
                break
    if not existing_description:
        existing_description = find_existing_description(soup)

    weight = extract_weight(soup, page_text, json_ld_items)
    spec_fields = extract_spec_fields(soup, page_text, json_ld_items, weight=weight)
    if not brand:
        brand = normalize_spec_value(spec_fields.get("Producent", "")) or normalize_spec_value(spec_fields.get("Marka", ""))

    raw_price = extract_price(soup, page_text, json_ld_items)
    source_currency = detect_currency(soup, page_text, json_ld_items, raw_html=html_doc)
    price, price_debug = convert_price_to_pln(raw_price, source_currency)
    variant_options, variant_debug = extract_variant_options(soup, raw_html=html_doc)
    available = extract_availability(soup, json_ld_items, page_text)
    images = extract_images(soup, final_url)

    scrape_debug = {
        "adapter": "olek",
        "fetch_method": fetch_method,
        "source_currency": source_currency,
        "source_price": raw_price,
        "exchange_rate_used": price_debug.get("exchange_rate_used"),
        "converted_price_pln_before_rounding": price_debug.get("converted_price_pln_before_rounding"),
        "final_price_pln_after_rounding": price_debug.get("final_price_pln_after_rounding"),
        "currency_conversion_warning": price_debug.get("conversion_warning", ""),
        "detected_variants": variant_options,
        "detected_sizes": variant_options,
        "variant_source": variant_debug.get("variant_source", ""),
        "sku_found": bool(sku),
        "ean_found": bool(ean),
        "images_found": len(images),
        "spec_found": bool(spec_fields),
        "availability_found": available is not None,
    }

    return {
        "url": final_url,
        "source_domain": extract_source_domain(final_url),
        "title": title,
        "page_text": page_text,
        "existing_description": existing_description,
        "price": price,
        "weight": weight,
        "available": available,
        "images": images,
        "sku": sku,
        "ean": ean,
        "brand": brand,
        "breadcrumb": breadcrumb,
        "source_category": source_category,
        "tags": brand,
        "vehicle_type": "",
        "homologation": "",
        "spec_fields": spec_fields,
        "variant_options": variant_options,
        "scrape_debug": scrape_debug,
    }


def is_sansa_product_url(url):
    parsed = urlparse(safe_str(url))
    return is_sansa_url(url) and re.search(r"/\d+-.+\.html$", parsed.path.lower()) is not None


def find_json_ld_product(json_ld_items):
    for data in json_ld_items:
        for item in flatten_json_ld(data):
            if not isinstance(item, dict):
                continue
            item_type = safe_str(item.get("@type", ""))
            if item_type.lower() == "product":
                return item
    return {}


def parse_sansa_reference_text(text):
    blob = normalize_whitespace(text)
    sku = ""
    ean = ""
    sku_match = re.search(r"SKU:\s*([A-Za-z0-9._/\-]+)", blob, flags=re.IGNORECASE)
    ean_match = re.search(r"EAN:\s*([0-9A-Za-z._/\-]+)", blob, flags=re.IGNORECASE)
    if sku_match:
        sku = normalize_whitespace(sku_match.group(1))
    if ean_match:
        ean = normalize_whitespace(ean_match.group(1))
    return sku, ean


def parse_weight_to_kg(value):
    text = normalize_spec_value(value)
    if not text:
        return None
    kg_match = re.search(r"(\d+(?:[.,]\d+)?)\s*kg\b", ascii_fold(text).lower())
    if kg_match:
        return parse_float(kg_match.group(1))
    gram_match = re.search(r"(\d+(?:[.,]\d+)?)\s*g\b", ascii_fold(text).lower())
    if gram_match:
        grams = parse_float(gram_match.group(1))
        if grams is not None:
            return grams / 1000.0
    return None


def extract_sansa_breadcrumb(soup, json_ld_items, title=""):
    crumbs = []
    seen = set()
    for data in json_ld_items:
        for item in flatten_json_ld(data):
            if not isinstance(item, dict) or safe_str(item.get("@type", "")).lower() != "breadcrumblist":
                continue
            elements = item.get("itemListElement", [])
            if not isinstance(elements, list):
                continue
            for element in sorted([el for el in elements if isinstance(el, dict)], key=lambda el: int(el.get("position", 999))):
                name = normalize_whitespace(html.unescape(safe_str(element.get("name", ""))))
                if not name:
                    continue
                folded = normalize_spec_label_key(name)
                if folded in seen:
                    continue
                seen.add(folded)
                crumbs.append(name)
    if not crumbs:
        for node in soup.select("nav.breadcrumb li, .breadcrumb li"):
            name = normalize_whitespace(node.get_text(" ", strip=True))
            if not name:
                continue
            folded = normalize_spec_label_key(name)
            if folded in seen:
                continue
            seen.add(folded)
            crumbs.append(name)
    breadcrumb = " / ".join(crumbs)
    title_key = normalize_spec_label_key(title)
    category_candidates = [
        crumb for crumb in crumbs
        if normalize_spec_label_key(crumb) not in {"strona glowna", title_key}
    ]
    source_category = category_candidates[-1] if category_candidates else ""
    return breadcrumb[:300], source_category


def extract_sansa_data_product(soup):
    node = soup.select_one("#product-details[data-product]")
    if not node or not node.get("data-product"):
        return {}
    try:
        return json.loads(node.get("data-product"))
    except Exception:
        return {}


def extract_sansa_section_heading(node):
    if node is None or getattr(node, "name", None) is None:
        return ""
    if node.name in {"h2", "h3", "h4"}:
        return normalize_whitespace(node.get_text(" ", strip=True))
    if node.name == "p":
        strong = node.find("strong")
        if strong:
            label = normalize_whitespace(strong.get_text(" ", strip=True))
            full_text = normalize_whitespace(node.get_text(" ", strip=True))
            if label and full_text == label:
                return label
    return ""


def extract_sansa_description_sections(description_container):
    intro_parts = []
    sections = []
    current_heading = ""
    current_parts = []
    for child in description_container.children:
        if getattr(child, "name", None) is None:
            continue
        heading = extract_sansa_section_heading(child)
        if heading:
            if current_heading:
                sections.append((current_heading, list(current_parts)))
            elif current_parts:
                intro_parts.extend(current_parts)
            current_heading = heading
            current_parts = []
            continue
        text = ""
        if child.name in {"ul", "ol"}:
            items = [normalize_whitespace(li.get_text(" ", strip=True)) for li in child.select("li")]
            items = [item for item in items if item]
            text = " | ".join(items)
        else:
            text = normalize_whitespace(child.get_text(" ", strip=True))
        if not text:
            continue
        if current_heading:
            current_parts.append(text)
        else:
            intro_parts.append(text)
    if current_heading:
        sections.append((current_heading, list(current_parts)))
    elif current_parts:
        intro_parts.extend(current_parts)

    # Dedupe repeated intro paragraphs while preserving order.
    intro_seen = set()
    intro_deduped = []
    for part in intro_parts:
        folded = normalize_spec_label_key(part)
        if not folded or folded in intro_seen:
            continue
        intro_seen.add(folded)
        intro_deduped.append(part)
    return normalize_whitespace(" ".join(intro_deduped)), sections


def extract_sansa_spec_fields(description_container):
    spec_fields = {}
    details_lines = []
    intro_text, sections = extract_sansa_description_sections(description_container)
    for heading, parts in sections:
        parts = [normalize_spec_value(part) for part in parts if normalize_spec_value(part)]
        if not parts:
            continue
        value = " | ".join(parts)
        details_lines.append(f"{heading}: {value}")
        upsert_spec_field(spec_fields, heading, value)
    return intro_text, normalize_whitespace(" ".join(details_lines)), normalize_spec_fields(spec_fields)


def extract_sansa_images(main_product, base_url):
    urls = []
    seen = set()
    selectors = [
        ".product-cover .js-easyzoom-trigger[href]",
        ".product-cover img[data-image-large-src]",
        ".product-cover img[data-full-size-image-url]",
        ".thumbnail.product-thumbnail img[data-full-size-image-url]",
        ".thumb-container img[data-image-large-src]",
    ]
    for selector in selectors:
        for node in main_product.select(selector):
            candidates = [
                node.get("href"),
                node.get("data-image-large-src"),
                node.get("data-full-size-image-url"),
                node.get("data-src"),
                node.get("src"),
            ]
            for candidate in candidates:
                if not candidate or candidate.startswith("data:image"):
                    continue
                full = urljoin(base_url, candidate)
                low = full.lower()
                if not low.startswith(("http://", "https://")):
                    continue
                if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", low) is None:
                    continue
                if full in seen:
                    continue
                seen.add(full)
                urls.append(full)
                if len(urls) >= MAX_IMAGES:
                    return urls
    return urls


def extract_sansa_price(main_product, data_product, json_ld_product):
    if data_product:
        show_price = safe_str(data_product.get("show_price", "")).strip()
        if show_price in {"0", "False", "false"}:
            return None, "data-product.hidden_or_missing"
        for raw in [data_product.get("price"), data_product.get("price_amount")]:
            price = parse_float(raw)
            if price is not None and price > 0:
                return price, "data-product.price"
    for selector in ["[itemprop='price']", ".current-price span", ".product-price", ".price"]:
        node = main_product.select_one(selector)
        if node:
            price = parse_float(node.get("content") or node.get_text(" ", strip=True))
            if price is not None and price > 0:
                return price, selector
    if isinstance(json_ld_product, dict):
        offers = json_ld_product.get("offers")
        if isinstance(offers, dict):
            price = parse_float(offers.get("price"))
            if price is not None and price > 0:
                return price, "jsonld.offers.price"
    return None, "not_public_or_not_found"


def extract_sansa_availability(soup, main_product, data_product):
    if data_product:
        available_for_order = data_product.get("available_for_order")
        if safe_str(available_for_order) in {"1", "true", "True"}:
            return True, "data-product.available_for_order"
        if safe_str(available_for_order) in {"0", "false", "False"}:
            return False, "data-product.available_for_order"
    body_classes = " ".join(soup.body.get("class", [])) if soup.body else ""
    if "product-available-for-order" in body_classes:
        return True, "body.product-available-for-order"
    text_blob = normalize_whitespace(main_product.get_text(" ", strip=True)).lower()
    if any(token in text_blob for token in ["brak w magazynie", "niedostepny", "niedostępny"]):
        return False, "product-text.unavailable"
    return None, "unknown"


def scrape_sansa_product(url):
    final_url, html_doc = fetch_html(url)
    soup = BeautifulSoup(html_doc, "html.parser")
    json_ld_items = extract_json_ld(soup)
    json_ld_product = find_json_ld_product(json_ld_items)
    data_product = extract_sansa_data_product(soup)
    main_product = soup.select_one("#main") or soup.body or soup

    title_node = main_product.select_one("h1")
    title = normalize_whitespace(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title:
        title = normalize_whitespace(html.unescape(safe_str(json_ld_product.get("name", ""))))

    reference_selector = ".product-information .product-reference"
    reference_node = main_product.select_one(reference_selector) or main_product.select_one(".product-reference")
    reference_text = normalize_whitespace(reference_node.get_text(" ", strip=True)) if reference_node else ""
    sku, ean = parse_sansa_reference_text(reference_text)
    if not sku:
        sku = normalize_whitespace(data_product.get("reference") or json_ld_product.get("sku") or json_ld_product.get("mpn"))
    if not ean:
        ean = normalize_whitespace(json_ld_product.get("gtin13") or data_product.get("ean13"))

    brand = ""
    brand_data = json_ld_product.get("brand") if isinstance(json_ld_product, dict) else {}
    if isinstance(brand_data, dict):
        brand = normalize_whitespace(brand_data.get("name", ""))
    if not brand:
        brand = normalize_whitespace(data_product.get("manufacturer_name"))
    if not brand:
        brand_node = main_product.select_one(".product-brand a, .product-brand, .product-manufacturer img[alt]")
        if brand_node:
            brand = normalize_whitespace(brand_node.get("alt") or brand_node.get_text(" ", strip=True))

    breadcrumb, source_category = extract_sansa_breadcrumb(soup, json_ld_items, title=title)

    description_selector = "#description .rte-content"
    description_container = main_product.select_one(description_selector)
    if description_container is None and data_product.get("description"):
        description_container = BeautifulSoup(data_product.get("description"), "html.parser")

    intro_text = ""
    details_text = ""
    spec_fields = {}
    if description_container is not None:
        intro_text, details_text, spec_fields = extract_sansa_spec_fields(description_container)

    images = extract_sansa_images(main_product, final_url)
    raw_price, price_source = extract_sansa_price(main_product, data_product, json_ld_product)
    source_currency = detect_currency(soup, page_text="", json_ld_items=json_ld_items, raw_html=html_doc, data_product=data_product)
    price, price_debug = convert_price_to_pln(raw_price, source_currency)
    available, availability_source = extract_sansa_availability(soup, main_product, data_product)
    weight = parse_weight_to_kg(spec_fields.get("Waga"))
    variant_options, variant_debug = extract_variant_options(main_product, raw_html=html_doc)

    page_text_parts = [
        title,
        brand,
        reference_text,
        breadcrumb,
        intro_text,
        details_text,
        spec_fields_as_text(spec_fields),
    ]
    page_text = normalize_whitespace(" ".join([part for part in page_text_parts if part]))

    scrape_debug = {
        "adapter": "sansa_product",
        "product_selector": "#main",
        "title_selector": "h1" if title_node else "jsonld.Product.name",
        "reference_selector": reference_selector if reference_node else "",
        "description_selector": description_selector if description_container is not None else "",
        "breadcrumb_selector": "jsonld.BreadcrumbList" if breadcrumb else "nav.breadcrumb",
        "image_selector": ".product-cover .js-easyzoom-trigger[href]",
        "sku_found": bool(sku),
        "ean_found": bool(ean),
        "brand_found": bool(brand),
        "images_found": len(images),
        "spec_found": bool(spec_fields),
        "spec_fields_count": len(spec_fields),
        "spec_fields_keys": list(spec_fields.keys()),
        "price_available": price is not None,
        "price_source": price_source,
        "source_currency": source_currency,
        "source_price": raw_price,
        "exchange_rate_used": price_debug.get("exchange_rate_used"),
        "converted_price_pln_before_rounding": price_debug.get("converted_price_pln_before_rounding"),
        "final_price_pln_after_rounding": price_debug.get("final_price_pln_after_rounding"),
        "currency_conversion_warning": price_debug.get("conversion_warning", ""),
        "detected_variants": variant_options,
        "detected_sizes": variant_options,
        "variant_source": variant_debug.get("variant_source", ""),
        "availability_source": availability_source,
    }

    return {
        "url": final_url,
        "source_domain": extract_source_domain(final_url),
        "title": title,
        "page_text": page_text,
        "existing_description": intro_text[:5000],
        "price": price,
        "weight": weight,
        "available": available,
        "images": images,
        "sku": sku,
        "ean": ean,
        "brand": brand,
        "breadcrumb": breadcrumb,
        "source_category": source_category,
        "tags": brand,
        "vehicle_type": "",
        "homologation": "",
        "spec_fields": spec_fields,
        "variant_options": variant_options,
        "scrape_debug": scrape_debug,
    }


def scrape_sansa_listing(url):
    final_url, html_doc = fetch_html(url)
    soup = BeautifulSoup(html_doc, "html.parser")
    card_selector = "#js-product-list .product-miniature, .products .product-miniature"
    cards = soup.select(card_selector)
    preview = []
    seen = set()
    for card in cards:
        title_node = card.select_one(".product-title a[href]")
        if title_node is None:
            continue
        href = title_node.get("href")
        full = urljoin(final_url, href)
        if not is_sansa_product_url(full) or full in seen:
            continue
        seen.add(full)
        title = normalize_whitespace(title_node.get_text(" ", strip=True)) or normalize_whitespace(title_node.get("title", ""))
        brand_node = card.select_one(".product-brand a, .product-brand")
        brand = normalize_whitespace(brand_node.get_text(" ", strip=True)) if brand_node else ""
        ref_text = " ".join([normalize_whitespace(node.get_text(" ", strip=True)) for node in card.select(".product-reference")])
        sku, ean = parse_sansa_reference_text(ref_text)
        preview.append({
            "url": full,
            "title": title or full,
            "brand": brand,
            "sku": sku,
            "ean": ean,
            "source_domain": extract_source_domain(full),
        })
        if len(preview) >= MAX_LISTING_PRODUCTS:
            break
    return preview, f"Sansa cards ({card_selector})"


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
    if is_sansa_url(url):
        return scrape_sansa_product(url)
    if is_olek_url(url):
        return scrape_olek_product(url)

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
    raw_price = extract_price(soup, page_text, json_ld_items)
    source_currency = detect_currency(soup, page_text, json_ld_items, raw_html=html)
    price, price_debug = convert_price_to_pln(raw_price, source_currency)
    variant_options, variant_debug = extract_variant_options(soup, raw_html=html)
    scrape_debug = {
        "source_currency": source_currency,
        "source_price": raw_price,
        "exchange_rate_used": price_debug.get("exchange_rate_used"),
        "converted_price_pln_before_rounding": price_debug.get("converted_price_pln_before_rounding"),
        "final_price_pln_after_rounding": price_debug.get("final_price_pln_after_rounding"),
        "currency_conversion_warning": price_debug.get("conversion_warning", ""),
        "detected_variants": variant_options,
        "detected_sizes": variant_options,
        "variant_source": variant_debug.get("variant_source", ""),
    }
    return {
        "url": final_url,
        "source_domain": extract_source_domain(final_url),
        "title": title,
        "page_text": page_text,
        "existing_description": find_existing_description(soup),
        "price": price,
        "weight": weight,
        "available": extract_availability(soup, json_ld_items, page_text),
        "images": extract_images(soup, final_url),
        "sku": sku,
        "ean": "",
        "brand": "",
        "breadcrumb": breadcrumb,
        "source_category": categories,
        "tags": tags,
        "vehicle_type": vehicle_type,
        "homologation": homologation,
        "spec_fields": spec_fields,
        "variant_options": variant_options,
        "scrape_debug": scrape_debug,
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


def canonicalize_product_url(href, base_url):
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    return parsed._replace(query="", fragment="").geturl()


def is_probable_product_url(href, base_url):
    if not href:
        return False
    href = urljoin(base_url, href)
    parsed = urlparse(href)
    if parsed.netloc != urlparse(base_url).netloc:
        return False
    if is_adidas_product_url(href):
        return True
    if is_olek_product_url(href):
        return True
    path_low = parsed.path.lower()
    if not any(marker in path_low for marker in ["/produkt/", "/products/", "/product/"]):
        return False
    blocked = [
        "javascript:",
        "/tag/",
        "/konto/",
        "/cart",
        "/koszyk",
        "/checkout",
        "/kontakt",
        "/blog/",
        "/collections/",
        "/pages/",
        "/search",
    ]
    low = href.lower()
    return not any(x in low for x in blocked)


@st.cache_data(show_spinner=False, ttl=3600)
def scrape_listing_products(url):
    if is_sansa_url(url):
        return scrape_sansa_listing(url)
    if is_olek_url(url):
        return scrape_olek_listing(url)

    final_url, html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    selectors = [
        ".products .product-card a.title-product-url",
        ".product-card a.title-product-url",
        ".products .product-card a[href*='/products/']",
        "ul.products li a",
        ".products .product a",
        ".product-item a",
        ".product-grid a",
        ".woocommerce-loop-product__link",
        ".product a",
    ]
    if is_adidas_url(final_url):
        selectors = [
            "[data-auto-id='product-card'] a[href$='.html']",
            "main a[href$='.html']",
            "a[href$='.html']",
        ] + selectors
    links = []
    preview = []
    selectors_tried = []
    for selector in selectors:
        selectors_tried.append(selector)
        for a in soup.select(selector):
            href = a.get("href")
            if is_probable_product_url(href, final_url):
                full = canonicalize_product_url(href, final_url)
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
                            full = canonicalize_product_url(href, final_url)
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
            all_links.append(canonicalize_product_url(href, final_url))
    detected = detect_product_pattern(all_links)
    if detected:
        return [{"url": u, "title": u} for u in detected[:MAX_LISTING_PRODUCTS]], "Pattern detection"
    raise ScrapeDiagnosticError(
        "Nie znaleziono kart produktowych na listingu.",
        debug={
            "stage": "scrape_listing_products",
            "source_domain": extract_source_domain(final_url),
            "requested_url": safe_str(url),
            "final_url": safe_str(final_url),
            "selectors_tried": selectors_tried,
            "json_ld_items_count": len(json_ld_items),
            "candidate_anchor_count": len(all_links),
            "response_title": extract_html_title(html),
            "response_preview": make_response_preview(html),
            "waf_detected": False,
            "waf_vendor": "",
        },
    )


def make_raw_response_preview(text, limit=2000):
    preview = safe_str(text).replace("\r", "")
    return preview[:limit]


def iter_json_object_candidates(text):
    text = safe_str(text)
    if not text:
        return
    seen = set()

    def _yield(candidate):
        candidate = safe_str(candidate).strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate

    for candidate in _yield(text):
        yield candidate

    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        for candidate in _yield(match.group(1)):
            yield candidate

    for start in [m.start() for m in re.finditer(r"\{", text)]:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    for candidate in _yield(text[start:idx + 1]):
                        yield candidate
                    break


def extract_json_object_from_text(text):
    last_error = None
    for candidate in iter_json_object_candidates(text):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, candidate, "candidate"
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError("Model nie zwrócił poprawnego obiektu JSON.")


class ModelJsonParseError(ValueError):
    def __init__(self, message, debug=None):
        super().__init__(message)
        self.debug = debug or {}


def request_model_json_repair(client, model, raw_text):
    repair_prompt = {
        "instruction": (
            "Napraw ponizsza odpowiedz do jednego poprawnego obiektu JSON. "
            "Zachowaj tresc, ale popraw skladnie. Zwróć wyłącznie czysty JSON."
        ),
        "raw_response": safe_str(raw_text),
    }
    message = client.messages.create(
        model=model,
        max_tokens=2600,
        temperature=0.0,
        system=JSON_REPAIR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": [{"type": "text", "text": json.dumps(repair_prompt, ensure_ascii=False)}]}],
    )
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")


def safe_parse_model_json(text, client=None, model=""):
    raw_text = safe_str(text)
    debug = {
        "raw_model_response_preview": make_raw_response_preview(raw_text),
        "parsed_json_success": False,
        "json_parse_error": "",
        "json_parse_strategy": "",
        "repair_attempted": False,
        "repair_success": False,
        "repair_response_preview": "",
    }

    try:
        parsed, _candidate, strategy = extract_json_object_from_text(raw_text)
        debug["parsed_json_success"] = True
        debug["json_parse_strategy"] = strategy
        return parsed, debug
    except Exception as exc:
        debug["json_parse_error"] = safe_str(exc)

    if client is not None and model:
        debug["repair_attempted"] = True
        try:
            repaired_text = request_model_json_repair(client, model, raw_text)
            debug["repair_response_preview"] = make_raw_response_preview(repaired_text)
            parsed, _candidate, strategy = extract_json_object_from_text(repaired_text)
            debug["parsed_json_success"] = True
            debug["repair_success"] = True
            debug["json_parse_strategy"] = f"repair:{strategy}"
            return parsed, debug
        except Exception as exc:
            debug["json_parse_error"] = safe_str(exc)

    raise ModelJsonParseError(
        f"Nie udalo sie sparsowac odpowiedzi modelu jako JSON. {debug['json_parse_error']}",
        debug=debug,
    )


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


def should_use_local_rewrite_copy(rewrite_mode, rewrite_copy_without_api):
    return bool(rewrite_mode and rewrite_copy_without_api)


def build_local_rewrite_content(product_data):
    source_html = safe_str(product_data.get("existing_description", ""))
    if not source_html:
        plain_source = normalize_whitespace(product_data.get("page_text", ""))
        source_html = f"<p>{html.escape(plain_source)}</p>" if plain_source else ""
    plain_text = html_to_plain_text(source_html)
    short_description = trim_text_excerpt(plain_text, 280)
    name = safe_str(product_data.get("name") or product_data.get("title") or "Produkt")
    return {
        "name": name,
        "short_description": short_description,
        "description": source_html,
        "seo_title": name,
        "seo_description": trim_text_excerpt(short_description or plain_text, 160),
        "seo_url": slugify(name),
        "model_debug": {
            "generation_mode": "local_rewrite_copy",
            "parsed_json_success": True,
            "json_parse_strategy": "not_used_local_copy",
            "raw_model_response_preview": "",
        },
    }


def generate_with_claude(client, model, rewrite_mode, product_data, uploaded_image=None, rewrite_copy_without_api=False):
    if should_use_local_rewrite_copy(rewrite_mode, rewrite_copy_without_api):
        return build_local_rewrite_content(product_data)
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
        "instruction": (
            "Wygeneruj zgodnie z system prompt. "
            "Zwróć wyłącznie jeden poprawny obiekt JSON bez markdownu, bez fenced block, bez komentarzy i bez dodatkowego tekstu. "
            "Jesli description zawiera HTML, ma pozostac poprawnym stringiem JSON."
        ),
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
    data, model_debug = safe_parse_model_json(text, client=client, model=model)
    model_debug["generation_mode"] = "rewrite_api" if rewrite_mode else "ai"
    for field in ["name", "short_description", "description", "seo_title", "seo_description", "seo_url"]:
        data.setdefault(field, "")
    if not data["seo_url"]:
        data["seo_url"] = slugify(data.get("name") or normalized.get("name"))
    data["model_debug"] = model_debug
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
OFFROAD_STRONG_TERMS = [
    "off-road",
    "off road",
    "full cross",
    "bez homologacji",
    "wersja bez homologacji",
    "brak homologacji",
    "brak ograniczen homologacyjnych",
    "brak ograniczeń homologacyjnych",
    "tylko do terenu",
    "tylko do jazdy w terenie",
    "off-road only",
    "tylko tor",
    "predkosc max (off-road)",
    "predkosc max off-road",
    "predkosc max off road",
    "prędkość max (off-road)",
]
OFFROAD_EQUIPMENT_GAP_TERMS = [
    "nie zawiera kierunkowskazow",
    "nie zawiera kierunkowskazów",
    "brak kierunkowskazow",
    "brak kierunkowskazów",
    "brak lusterek",
    "brak klaksonu",
    "brak miejsca na tablice",
    "brak miejsca na tablicę",
]
POSITIVE_ROAD_HOMO_TERMS = [
    "homologacja l1e",
    "homologacja l3e",
    "wersja drogowa",
    "dopuszczony do ruchu drogowego",
    "dopuszczony do ruchu",
    "mozliwosc rejestracji",
    "możliwość rejestracji",
    "homologacja drogowa",
    "road legal",
    "street legal",
]
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
    direct_l1e_signal = bool(re.search(r"\bl1e\b", product_focus_text)) or bool(re.search(r"\bl1e\b", ascii_fold(homologation).lower()))
    direct_l3e_signal = bool(re.search(r"\bl3e\b", product_focus_text)) or bool(re.search(r"\bl3e\b", ascii_fold(homologation).lower()))
    direct_l6e_signal = bool(re.search(r"\bl6e\b", product_focus_text)) or bool(re.search(r"\bl6e\b", ascii_fold(homologation).lower()))
    direct_l7e_signal = bool(re.search(r"\bl7e\b", product_focus_text)) or bool(re.search(r"\bl7e\b", ascii_fold(homologation).lower()))
    homologation_raw_text = ascii_fold(homologation).lower()
    homologation_yes_field = bool(re.search(r"\b(tak|yes|road legal|street legal|homologacja drogowa)\b", homologation_raw_text))
    homologation_no_field = bool(re.search(r"\b(nie|no|brak|bez homologacji|off-?road|tylko tor)\b", homologation_raw_text))
    homologation_declared = has_any(product_focus_text, [
        "homologacja drogowa",
        "dopuszczony do ruchu",
        "legalnie po drogach",
        "legalnego poruszania sie po drogach",
    ] + POSITIVE_ROAD_HOMO_TERMS) or homologation_yes_field
    no_homologation_declared = has_any(product_focus_text, [
        "bez homologacji",
        "brak homologacji",
        "wersja bez homologacji",
        "brak ograniczen homologacyjnych",
        "brak ograniczeń homologacyjnych",
        "off-road only",
        "tylko do terenu",
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
    offroad_title_signal = has_any(title_text, ["off-road", "off road", "full cross"])
    offroad_equipment_gap_signal = has_any(product_focus_text, OFFROAD_EQUIPMENT_GAP_TERMS)
    strong_offroad_signal = offroad_title_signal or has_any(product_focus_text, OFFROAD_STRONG_TERMS) or offroad_equipment_gap_signal
    offroad_signal = strong_offroad_signal or has_any(product_focus_text, ["off-road", "offroad", "bez homologacji", "full cross", "tylko tor"])
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
    positive_road_equipment_signal = (
        has_any(product_focus_text, ["kierunkowskazy", "lusterka", "miejsce na tablice", "miejsce na tablicę", "klakson"])
        and not offroad_equipment_gap_signal
    )
    direct_road_homologation_signal = any([
        direct_l1e_signal,
        direct_l3e_signal,
        direct_l6e_signal,
        direct_l7e_signal,
        homologation_declared,
        positive_road_equipment_signal,
    ])
    strong_no_homologation_signal = strong_offroad_signal or no_homologation_declared

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

    if strong_no_homologation_signal and not direct_road_homologation_signal:
        if has_l1e and not direct_l1e_signal:
            has_l1e = False
            conflict_rejections["l1e"] = "rejected_by_strong_offroad_without_direct_homologation"
        if has_l3e and not direct_l3e_signal:
            has_l3e = False
            conflict_rejections["l3e"] = "rejected_by_strong_offroad_without_direct_homologation"
        if scooter_signal and not title_scooter_signal:
            scooter_signal = False
            conflict_rejections["scooter"] = "rejected_by_strong_offroad_without_direct_homologation"
        road_homologation = False
        strong_road_homologation = False
    elif strong_no_homologation_signal and direct_road_homologation_signal:
        conflict_rejections["offroad_override"] = "blocked_by_direct_road_homologation_signal"

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
        "direct_l1e_signal": direct_l1e_signal,
        "direct_l3e_signal": direct_l3e_signal,
        "direct_l6e_signal": direct_l6e_signal,
        "direct_l7e_signal": direct_l7e_signal,
        "road_homologation": road_homologation,
        "strong_road_homologation": strong_road_homologation,
        "direct_road_homologation_signal": direct_road_homologation_signal,
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
        "offroad_title_signal": offroad_title_signal,
        "strong_offroad_signal": strong_offroad_signal,
        "strong_no_homologation_signal": strong_no_homologation_signal,
        "offroad_equipment_gap_signal": offroad_equipment_gap_signal,
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
        "rejected_l1e_reason": "",
        "rejected_l3e_reason": "",
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
        if has_l3e and direct_l3e_signal:
            return category_decision("Skutery elektryczne > Skuter 125 cm³ (L3e)", 0.97, "scooter_l3e_homologation", signals)
        if has_l3e and not direct_l3e_signal:
            signals["rejected_l3e_reason"] = "missing_direct_product_l3e_signal"
        if has_l1e and direct_l1e_signal:
            return category_decision("Skutery elektryczne > Skuter 50 cm³ (L1e)", 0.96, "scooter_l1e_homologation", signals)
        if has_l1e and not direct_l1e_signal:
            signals["rejected_l1e_reason"] = "missing_direct_product_l1e_signal"
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

        if strong_no_homologation_signal and not direct_road_homologation_signal:
            if not signals["rejected_l3e_reason"]:
                signals["rejected_l3e_reason"] = "blocked_by_strong_offroad_without_direct_homologation"
            if not signals["rejected_l1e_reason"]:
                signals["rejected_l1e_reason"] = "blocked_by_strong_offroad_without_direct_homologation"
            return category_decision(
                "Motocykle enduro elektryczne > Off-road / bez homologacji (full cross)",
                0.98,
                "enduro_strong_offroad_without_homologation_priority",
                signals,
            )

        if has_l3e and direct_l3e_signal:
            return category_decision("Motocykle enduro elektryczne > L3e / do 125 cm³", 0.97, "enduro_l3e_homologation", signals)
        if has_l3e and not direct_l3e_signal:
            signals["rejected_l3e_reason"] = "missing_direct_product_l3e_signal"
        if has_l1e and direct_l1e_signal:
            return category_decision("Motocykle enduro elektryczne > L1e / do 50 cm³", 0.95, "enduro_l1e_homologation", signals)
        if has_l1e and not direct_l1e_signal:
            signals["rejected_l1e_reason"] = "missing_direct_product_l1e_signal"
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
        "olek_browser_trace": [],
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
        rewrite_copy_without_api = st.checkbox(
            "Rewrite 1:1 bez API",
            value=False,
            help="Działa tylko w trybie rewrite. Kopiuje istniejący opis bez wywołania Claude API.",
        )
        with st.expander("Sesja Cloudflare", expanded=False):
            st.caption("Dla sklepów blokowanych przez Cloudflare, np. Olek Motocykle, otwiera dedykowaną sesję Chrome z trwałym profilem.")
            if st.button("Otwórz sesję Olek", use_container_width=True):
                try:
                    launch_info = launch_olek_browser_session("https://shop.olekmotocykle.com/produkty,2")
                    if launch_info.get("launched"):
                        st.info("Otworzono dedykowaną sesję Chrome dla Olek. Przejdź challenge Cloudflare w tym oknie.")
                    else:
                        st.info("Sesja Olek jest już uruchomiona. Przejdź challenge Cloudflare w tym samym oknie Chrome.")
                except Exception as exc:
                    st.error(f"Nie udało się otworzyć sesji Olek: {safe_str(exc)}")
        st.checkbox("Debug mode", key="debug_mode")
        if st.button("Wyczyść wyniki", use_container_width=True):
            st.session_state.results = []
            st.session_state.product_code_counter = 0
        return model, rewrite_mode, rewrite_copy_without_api


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


def resolve_product_code(scraped_data, name, use_full_name_as_product_code=False):
    source_domain = safe_str(scraped_data.get("source_domain", "")).lower()
    sku = normalize_whitespace(scraped_data.get("sku", ""))
    sku_found = bool(sku)
    if is_sansa_url(source_domain) and sku_found:
        return sku, {
            "product_code_mode": "sku",
            "source_product_code": "sku",
            "sku_found": True,
            "final_product_code": sku,
        }

    if use_full_name_as_product_code:
        full_name_code = normalize_whitespace(name)
        return full_name_code, {
            "product_code_mode": "full_name",
            "source_product_code": "full_name",
            "sku_found": sku_found,
            "final_product_code": full_name_code,
        }

    fallback_code = next_product_code(name)
    return fallback_code, {
        "product_code_mode": "generated",
        "source_product_code": "fallback_counter",
        "sku_found": sku_found,
        "final_product_code": fallback_code,
    }


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
    "source_domain": "",
    "page_text": "",
    "existing_description": "",
    "price": None,
    "weight": None,
    "available": None,
    "images": [],
    "downloaded_images": [],
    "downloaded_images_dir": "",
    "sku": "",
    "ean": "",
    "brand": "",
    "breadcrumb": "",
    "source_category": "",
    "tags": "",
    "vehicle_type": "",
    "homologation": "",
    "spec_fields": {},
    "variant_options": [],
    "scrape_debug": {},
}

GENERATED_CONTENT_DEFAULTS = {
    "name": "",
    "short_description": "",
    "description": "",
    "seo_title": "",
    "seo_description": "",
    "seo_url": "",
    "model_debug": {},
}

RESULT_ITEM_DEFAULTS = {
    "url": "",
    "source_domain": "",
    "name": "",
    "product_code": "",
    "category": "",
    "producer": "",
    "weight": None,
    "price": None,
    "available": None,
    "images": [],
    "downloaded_images": [],
    "downloaded_images_dir": "",
    "sku": "",
    "ean": "",
    "brand": "",
    "short_description": "",
    "description": "",
    "seo_title": "",
    "seo_description": "",
    "seo_url": "",
    "gauge": "",
    "availability": "",
    "delivery": "",
    "price_buying": "",
    "buying_discount": 0.0,
    "spec_fields": {},
    "variant_options": [],
    "scrape_debug": {},
    "model_debug": {},
    "category_reason": "",
    "category_confidence": 0.0,
    "category_method": "",
    "category_score": None,
    "category_signals": {},
    "vinted_external_id": "",
    "vinted_description_mode": "ai",
    "vinted_category_group": "",
    "vinted_category_type": "",
    "vinted_brand": "",
    "vinted_size": "",
    "vinted_condition": "",
    "vinted_color": "",
    "vinted_material": "",
    "vinted_currency": "PLN",
    "vinted_package_size": "",
    "vinted_publish": True,
}


def normalize_scraped_product(data):
    merged = dict(SCRAPED_PRODUCT_DEFAULTS)
    if isinstance(data, dict):
        for key in merged:
            if key in data:
                merged[key] = data.get(key)
    merged["url"] = safe_str(merged.get("url", ""))
    merged["title"] = safe_str(merged.get("title", ""))
    merged["source_domain"] = safe_str(merged.get("source_domain", "")) or urlparse(merged["url"]).netloc.lower()
    merged["page_text"] = safe_str(merged.get("page_text", ""))
    merged["existing_description"] = safe_str(merged.get("existing_description", ""))
    merged["price"] = parse_float(merged.get("price"))
    merged["weight"] = parse_float(merged.get("weight"))
    merged["sku"] = safe_str(merged.get("sku", ""))
    merged["ean"] = safe_str(merged.get("ean", ""))
    merged["brand"] = safe_str(merged.get("brand", ""))
    merged["breadcrumb"] = safe_str(merged.get("breadcrumb", ""))
    merged["source_category"] = safe_str(merged.get("source_category", ""))
    merged["tags"] = safe_str(merged.get("tags", ""))
    merged["vehicle_type"] = safe_str(merged.get("vehicle_type", ""))
    merged["homologation"] = safe_str(merged.get("homologation", ""))
    merged["spec_fields"] = normalize_spec_fields(merged.get("spec_fields", {}))
    merged["variant_options"] = normalize_variant_options(merged.get("variant_options", []))
    merged["scrape_debug"] = merged.get("scrape_debug") if isinstance(merged.get("scrape_debug"), dict) else {}
    images = merged.get("images") or []
    if not isinstance(images, list):
        images = [images]
    merged["images"] = [safe_str(url) for url in images if safe_str(url)][:MAX_IMAGES]
    downloaded_images = merged.get("downloaded_images") or []
    if not isinstance(downloaded_images, list):
        downloaded_images = [downloaded_images]
    merged["downloaded_images"] = [safe_str(path) for path in downloaded_images if safe_str(path)]
    merged["downloaded_images_dir"] = safe_str(merged.get("downloaded_images_dir", ""))
    return merged


def normalize_generated_content(data):
    merged = dict(GENERATED_CONTENT_DEFAULTS)
    if isinstance(data, dict):
        for key in merged:
            if key in data:
                merged[key] = data.get(key)
    for key in ["name", "short_description", "description", "seo_title", "seo_description", "seo_url"]:
        merged[key] = safe_str(merged.get(key, ""))
    merged["model_debug"] = merged.get("model_debug") if isinstance(merged.get("model_debug"), dict) else {}
    return merged


def normalize_result_item(data):
    merged = dict(RESULT_ITEM_DEFAULTS)
    if isinstance(data, dict):
        for key in merged:
            if key in data:
                merged[key] = data.get(key)
    merged["url"] = safe_str(merged.get("url", ""))
    merged["source_domain"] = safe_str(merged.get("source_domain", "")) or urlparse(merged["url"]).netloc.lower()
    merged["name"] = safe_str(merged.get("name", ""))
    merged["product_code"] = safe_str(merged.get("product_code", ""))
    merged["category"] = safe_str(merged.get("category", ""))
    merged["producer"] = safe_str(merged.get("producer", ""))
    merged["weight"] = parse_float(merged.get("weight"))
    merged["price"] = parse_float(merged.get("price"))
    merged["sku"] = safe_str(merged.get("sku", ""))
    merged["ean"] = safe_str(merged.get("ean", ""))
    merged["brand"] = safe_str(merged.get("brand", ""))
    merged["short_description"] = safe_str(merged.get("short_description", ""))
    merged["description"] = safe_str(merged.get("description", ""))
    merged["seo_title"] = safe_str(merged.get("seo_title", ""))
    merged["seo_description"] = safe_str(merged.get("seo_description", ""))
    merged["seo_url"] = safe_str(merged.get("seo_url", ""))
    merged["gauge"] = safe_str(merged.get("gauge", ""))
    merged["availability"] = safe_str(merged.get("availability", ""))
    merged["delivery"] = normalize_delivery_days(merged.get("delivery", ""))
    merged["buying_discount"] = parse_float(merged.get("buying_discount")) or 0.0
    merged["spec_fields"] = normalize_spec_fields(merged.get("spec_fields", {}))
    merged["variant_options"] = normalize_variant_options(merged.get("variant_options", []))
    merged["scrape_debug"] = merged.get("scrape_debug") if isinstance(merged.get("scrape_debug"), dict) else {}
    merged["model_debug"] = merged.get("model_debug") if isinstance(merged.get("model_debug"), dict) else {}
    merged["category_reason"] = safe_str(merged.get("category_reason", ""))
    merged["category_method"] = safe_str(merged.get("category_method", ""))
    merged["category_confidence"] = parse_float(merged.get("category_confidence")) or 0.0
    merged["category_score"] = parse_float(merged.get("category_score"))
    merged["category_signals"] = merged.get("category_signals") if isinstance(merged.get("category_signals"), dict) else {}
    merged["vinted_external_id"] = safe_str(merged.get("vinted_external_id", ""))
    merged["vinted_description_mode"] = safe_str(merged.get("vinted_description_mode", "")) or "ai"
    merged["vinted_category_group"] = safe_str(merged.get("vinted_category_group", ""))
    merged["vinted_category_type"] = safe_str(merged.get("vinted_category_type", ""))
    merged["vinted_brand"] = safe_str(merged.get("vinted_brand", ""))
    merged["vinted_size"] = safe_str(merged.get("vinted_size", ""))
    merged["vinted_condition"] = safe_str(merged.get("vinted_condition", ""))
    merged["vinted_color"] = safe_str(merged.get("vinted_color", ""))
    merged["vinted_material"] = safe_str(merged.get("vinted_material", ""))
    merged["vinted_currency"] = safe_str(merged.get("vinted_currency", "")) or "PLN"
    merged["vinted_package_size"] = safe_str(merged.get("vinted_package_size", ""))
    merged["vinted_publish"] = bool(merged.get("vinted_publish", True))
    if merged.get("price_buying") == "":
        merged["price_buying"] = compute_price_buying(merged.get("price"), merged["buying_discount"])
    else:
        merged["price_buying"] = safe_str(merged.get("price_buying", ""))
    images = merged.get("images") or []
    if not isinstance(images, list):
        images = [images]
    merged["images"] = [safe_str(url) for url in images if safe_str(url)][:MAX_IMAGES]
    downloaded_images = merged.get("downloaded_images") or []
    if not isinstance(downloaded_images, list):
        downloaded_images = [downloaded_images]
    merged["downloaded_images"] = [safe_str(path) for path in downloaded_images if safe_str(path)]
    merged["downloaded_images_dir"] = safe_str(merged.get("downloaded_images_dir", ""))
    return merged


def build_generation_payload(scraped, category="", producer="", keywords="", features="", sku_override=""):
    scraped_data = normalize_scraped_product(scraped)
    spec_fields = normalize_spec_fields(scraped_data.get("spec_fields", {}))
    variant_options = normalize_variant_options(scraped_data.get("variant_options", []))
    payload = {
        "name": scraped_data.get("title", ""),
        "url": scraped_data.get("url", ""),
        "source_domain": scraped_data.get("source_domain", ""),
        "category": category,
        "producer": producer,
        "brand": scraped_data.get("brand", ""),
        "keywords": keywords,
        "features": features,
        "price": scraped_data.get("price", ""),
        "weight": scraped_data.get("weight", ""),
        "sku": sku_override or scraped_data.get("sku", ""),
        "ean": scraped_data.get("ean", ""),
        "availability": scraped_data.get("available", ""),
        "existing_description": scraped_data.get("existing_description", "")[:3000],
        "page_text": scraped_data.get("page_text", "")[:6000],
        "spec_fields": spec_fields,
        "spec_fields_text": spec_fields_as_text(spec_fields),
        "variant_options": variant_options,
        "variant_options_text": ", ".join(variant_options),
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


def append_olek_trace(event, **payload):
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": safe_str(event),
    }
    for key, value in payload.items():
        if isinstance(value, (dict, list, tuple)):
            entry[key] = value
        else:
            entry[key] = safe_str(value)
    append_error_log("olek_browser_trace", entry, max_items=300)


def log_scraping_error(url, exc):
    debug = getattr(exc, "debug", {}) if exc is not None else {}
    append_error_log(
        "scraping_errors",
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url": safe_str(url),
            "error": safe_str(exc),
            "stage": safe_str(debug.get("stage", "")),
            "status_code": safe_str(debug.get("status_code", "")),
            "source_domain": safe_str(debug.get("source_domain", "")),
            "final_url": safe_str(debug.get("final_url", "")),
            "response_title": safe_str(debug.get("response_title", "")),
            "waf_detected": bool(debug.get("waf_detected")),
            "waf_vendor": safe_str(debug.get("waf_vendor", "")),
            "selectors_tried": debug.get("selectors_tried") if isinstance(debug.get("selectors_tried"), list) else [],
            "json_ld_items_count": debug.get("json_ld_items_count"),
            "candidate_anchor_count": debug.get("candidate_anchor_count"),
            "response_preview": safe_str(debug.get("response_preview", ""))[:2000],
        },
    )


def log_generation_error(url, exc):
    debug = getattr(exc, "debug", {}) if exc is not None else {}
    append_error_log(
        "generation_errors",
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url": safe_str(url),
            "error": safe_str(exc),
            "raw_model_response_preview": safe_str(debug.get("raw_model_response_preview", ""))[:2000],
            "parsed_json_success": bool(debug.get("parsed_json_success")),
            "json_parse_error": safe_str(debug.get("json_parse_error", "")),
            "json_parse_strategy": safe_str(debug.get("json_parse_strategy", "")),
            "repair_attempted": bool(debug.get("repair_attempted")),
            "repair_success": bool(debug.get("repair_success")),
            "repair_response_preview": safe_str(debug.get("repair_response_preview", ""))[:2000],
        },
    )


def result_widget_suffix(item, idx):
    base = safe_str(item.get("product_code") or item.get("url") or item.get("name") or f"item-{idx}")
    folded = re.sub(r"[^a-z0-9]+", "-", ascii_fold(base).lower()).strip("-")
    if not folded:
        folded = f"item-{idx}"
    return f"{folded}-{idx}"


def default_vinted_external_id(index):
    return f"VT-{index+1:04d}"


def infer_vinted_description_mode(generated_data):
    generation_mode = safe_str((generated_data.get("model_debug") or {}).get("generation_mode", "")).lower()
    if "rewrite" in generation_mode:
        return "rewrite"
    if generation_mode == "manual":
        return "manual"
    return "ai"


def infer_vinted_size(variant_options):
    options = normalize_variant_options(variant_options)
    return options[0] if len(options) == 1 else ""


def build_common_result(scraped, generated, manual_category, producer, discount, gauge="", availability="", delivery="", use_full_name_as_product_code=False):
    scraped_data = normalize_scraped_product(scraped)
    generated_data = normalize_generated_content(generated)
    spec_fields = normalize_spec_fields(scraped_data.get("spec_fields", {}))
    variant_options = normalize_variant_options(scraped_data.get("variant_options", []))
    name = generated_data.get("name") or scraped_data.get("title") or "Produkt"
    description_with_specs = attach_specification_block(generated_data.get("description", ""), spec_fields)
    description_with_specs = attach_variant_options_block(description_with_specs, variant_options)
    product_code, product_code_debug = resolve_product_code(
        scraped_data,
        name,
        use_full_name_as_product_code=use_full_name_as_product_code,
    )
    scrape_debug = dict(scraped_data.get("scrape_debug", {}))
    scrape_debug.update(product_code_debug)
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
        "source_domain": safe_str(scraped_data.get("source_domain", "")),
        "name": safe_str(name),
        "product_code": product_code,
        "category": resolved_category,
        "producer": safe_str(producer or ""),
        "weight": parse_float(scraped_data.get("weight")),
        "price": price,
        "available": scraped_data.get("available"),
        "images": scraped_data.get("images", []),
        "downloaded_images": scraped_data.get("downloaded_images", []),
        "downloaded_images_dir": scraped_data.get("downloaded_images_dir", ""),
        "sku": safe_str(scraped_data.get("sku", "")),
        "ean": safe_str(scraped_data.get("ean", "")),
        "brand": safe_str(scraped_data.get("brand", "")),
        "short_description": safe_str(generated_data.get("short_description", "")),
        "description": safe_str(description_with_specs),
        "seo_title": safe_str(generated_data.get("seo_title", "")),
        "seo_description": safe_str(generated_data.get("seo_description", "")),
        "seo_url": safe_str(generated_data.get("seo_url", slugify(name))),
        "gauge": safe_str(gauge),
        "availability": safe_str(availability),
        "delivery": normalize_delivery_days(delivery),
        "price_buying": compute_price_buying(price, discount),
        "buying_discount": parse_float(discount) or 0.0,
        "spec_fields": spec_fields,
        "variant_options": variant_options,
        "scrape_debug": scrape_debug,
        "model_debug": generated_data.get("model_debug", {}),
        "category_reason": safe_str(category_meta.get("reason", "")),
        "category_confidence": parse_float(category_meta.get("confidence")) or 0.0,
        "category_method": safe_str(category_meta.get("method", "")),
        "category_score": category_meta.get("score"),
        "category_signals": category_meta.get("signals", {}),
        "vinted_external_id": "",
        "vinted_description_mode": infer_vinted_description_mode(generated_data),
        "vinted_category_group": "",
        "vinted_category_type": "",
        "vinted_brand": safe_str(producer or scraped_data.get("brand", "")),
        "vinted_size": infer_vinted_size(variant_options),
        "vinted_condition": "",
        "vinted_color": "",
        "vinted_material": safe_str(spec_fields.get("Materiały", "")),
        "vinted_currency": "PLN",
        "vinted_package_size": "",
        "vinted_publish": True,
    }
    return normalize_result_item(result)


# ==============================
# CSV / Export
# ==============================
EXPORT_HEADERS = [
    "product_code",
    "vat",
    "unit",
    "category",
    "producer",
    "weight",
    "active",
    "name",
    "short_description",
    "description",
    "price",
    "stock",
] + [f"images {i}" for i in range(1, 33)] + [
    "seo_title",
    "seo_description",
    "seo_url",
    "price_buying",
    "gauge",
    "availability",
    "delivery",
]

VINTED_BULK_HEADERS = [
    "external_id",
    "title",
    "description_mode",
    "description",
    "category_group",
    "category_type",
    "brand",
    "size",
    "condition",
    "color",
    "material",
    "price",
    "currency",
    "photo_1",
    "photo_2",
    "photo_3",
    "photo_4",
    "package_size",
    "sku",
    "publish",
]

VINTED_CATEGORY_GROUP_OPTIONS = ["", "men", "women", "kids", "home", "electronics", "entertainment", "hobbies", "sport"]
VINTED_DESCRIPTION_MODE_OPTIONS = ["manual", "rewrite", "ai"]
VINTED_CONDITION_OPTIONS = ["", "new_with_tags", "new_without_tags", "very_good", "good", "satisfactory"]
VINTED_COLOR_OPTIONS = ["", "black", "white", "blue", "red", "green", "gray", "beige", "brown", "pink", "purple", "yellow", "orange", "multicolor", "navy", "khaki", "silver", "gold"]
VINTED_PACKAGE_SIZE_OPTIONS = ["", "small", "medium", "large"]
VINTED_REQUIRED_FIELDS = [
    "external_id",
    "title",
    "description_mode",
    "category_group",
    "category_type",
    "brand",
    "size",
    "condition",
    "color",
    "price",
    "currency",
    "photo_1",
    "publish",
]


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
            "product_code": normalize_whitespace(item.get("product_code", "")),
            "vat": "23%",
            "unit": "szt.",
            "category": normalize_category(item.get("category", "")),
            "producer": normalize_whitespace(item.get("producer", "")),
            "weight": "" if weight is None else f"{weight}".replace(".", ","),
            "active": 1,
            "name": normalize_whitespace(item.get("name", "")),
            "short_description": sanitize_html_for_csv(item.get("short_description", "")),
            "description": sanitize_html_for_csv(item.get("description", "")),
            "price": "" if price is None else f"{price:.2f}".replace(".", ","),
            "stock": stock,
            "seo_title": normalize_whitespace(item.get("seo_title", "")),
            "seo_description": normalize_whitespace(item.get("seo_description", "")),
            "seo_url": normalize_whitespace(item.get("seo_url", "")),
            "price_buying": safe_str(str(price_buying).replace(".", ",")),
            "gauge": normalize_whitespace(item.get("gauge", "")),
            "availability": normalize_whitespace(item.get("availability", "")),
            "delivery": normalize_delivery_days(item.get("delivery", "")),
        }
        images = item.get("images") or []
        for idx in range(32):
            row[f"images {idx+1}"] = normalize_whitespace(images[idx]) if idx < len(images) else ""
        rows.append(row)
    return rows


def export_csv_bytes(results):
    deduped = dedupe_results(results)
    if len(EXPORT_HEADERS) != 51:
        raise RuntimeError(f"Nieprawidlowa liczba kolumn CSV: {len(EXPORT_HEADERS)} (oczekiwano 51)")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_HEADERS, delimiter=";", quoting=csv.QUOTE_ALL, extrasaction="ignore")
    writer.writeheader()
    for row in to_shoper_rows(deduped):
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def to_relative_export_path(path):
    text = safe_str(path)
    if not text:
        return ""
    try:
        abs_path = Path(text).resolve()
        cwd_path = Path.cwd().resolve()
        return abs_path.relative_to(cwd_path).as_posix()
    except Exception:
        return text


def pick_vinted_photo_values(item):
    local_images = item.get("downloaded_images") or []
    if local_images:
        return [to_relative_export_path(path) for path in local_images[:4]]
    images = item.get("images") or []
    return [safe_str(url) for url in images[:4] if safe_str(url)]


def normalize_vinted_price(value):
    price = parse_float(value)
    if price is None or price <= 0:
        return ""
    rounded = int(round(price))
    return str(rounded)


def map_product_to_vinted_csv_row(item, index):
    normalized = normalize_result_item(item)
    photo_values = pick_vinted_photo_values(normalized)
    description_mode = safe_str(normalized.get("vinted_description_mode", "")) or "ai"
    plain_description = html_to_plain_text(normalized.get("description", "") or normalized.get("short_description", ""))
    row = {
        "external_id": safe_str(normalized.get("vinted_external_id", "")).strip() or default_vinted_external_id(index),
        "title": normalize_whitespace(normalized.get("name", "")),
        "description_mode": description_mode,
        "description": plain_description,
        "category_group": safe_str(normalized.get("vinted_category_group", "")).strip(),
        "category_type": safe_str(normalized.get("vinted_category_type", "")).strip(),
        "brand": safe_str(normalized.get("vinted_brand", "")).strip() or safe_str(normalized.get("producer", "")).strip() or safe_str(normalized.get("brand", "")).strip(),
        "size": safe_str(normalized.get("vinted_size", "")).strip(),
        "condition": safe_str(normalized.get("vinted_condition", "")).strip(),
        "color": safe_str(normalized.get("vinted_color", "")).strip(),
        "material": safe_str(normalized.get("vinted_material", "")).strip(),
        "price": normalize_vinted_price(normalized.get("price")),
        "currency": safe_str(normalized.get("vinted_currency", "")).strip() or "PLN",
        "photo_1": photo_values[0] if len(photo_values) > 0 else "",
        "photo_2": photo_values[1] if len(photo_values) > 1 else "",
        "photo_3": photo_values[2] if len(photo_values) > 2 else "",
        "photo_4": photo_values[3] if len(photo_values) > 3 else "",
        "package_size": safe_str(normalized.get("vinted_package_size", "")).strip(),
        "sku": safe_str(normalized.get("sku", "")).strip(),
        "publish": "true" if normalized.get("vinted_publish", True) else "false",
    }
    return row


def validate_vinted_csv_row(row, index):
    errors = []
    for field in VINTED_REQUIRED_FIELDS:
        if not safe_str(row.get(field, "")).strip():
            errors.append(f"wiersz {index+1}: brak pola {field}")
    if row.get("description_mode") not in set(VINTED_DESCRIPTION_MODE_OPTIONS):
        errors.append(f"wiersz {index+1}: nieprawidłowe description_mode")
    if row.get("category_group") not in set(VINTED_CATEGORY_GROUP_OPTIONS[1:]):
        errors.append(f"wiersz {index+1}: nieprawidłowe category_group")
    if row.get("condition") not in set(VINTED_CONDITION_OPTIONS[1:]):
        errors.append(f"wiersz {index+1}: nieprawidłowe condition")
    if row.get("publish") not in {"true", "false"}:
        errors.append(f"wiersz {index+1}: nieprawidłowe publish")
    if row.get("currency") != "PLN":
        errors.append(f"wiersz {index+1}: currency musi mieć wartość PLN")
    if not re.fullmatch(r"\d+", safe_str(row.get("price", "")).strip()):
        errors.append(f"wiersz {index+1}: price musi być liczbą całkowitą bez waluty")
    return errors


def build_vinted_export_rows(results):
    rows = []
    errors = []
    for index, raw_item in enumerate(dedupe_results(results)):
        row = map_product_to_vinted_csv_row(raw_item, index)
        rows.append(row)
        errors.extend(validate_vinted_csv_row(row, index))
    return rows, errors


def export_vinted_bulk_csv_bytes(results):
    rows, errors = build_vinted_export_rows(results)
    if errors:
        raise ValueError("Walidacja eksportu vinted-bulk-uploader nie powiodła się.")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=VINTED_BULK_HEADERS, delimiter=",", quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8"), rows


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


def tab_links(model, rewrite_mode, rewrite_copy_without_api):
    st.subheader("Z linków produktów")
    gauge_options = ["", "Towar", "Pojazd", "Pojazd dla dzieci"]
    availability_options = ["", "Auto"]
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
            extra1, extra2, extra3 = st.columns(3)
            with extra1:
                gauge = st.selectbox("Gauge", gauge_options, key="links_gauge")
            with extra2:
                availability = st.selectbox("Availability", availability_options, key="links_availability")
            with extra3:
                delivery = st.text_input("Delivery (liczba dni)", key="links_delivery")
            download_images = st.checkbox(
                "Pobierz zdjęcia produktu (JPG/PNG)",
                value=False,
                key="links_download_images",
            )
            use_full_name_as_product_code = st.checkbox(
                "Użyj pełnej nazwy produktu jako product_code",
                value=False,
                key="links_product_code_full_name",
            )
        submit_links = st.form_submit_button("Generuj dla URL-i", type="primary", use_container_width=True)
    if submit_links:
        urls = parse_urls(urls_raw)
        if not urls:
            st.warning("Dodaj co najmniej jeden poprawny URL.")
            return
        client = None
        if not should_use_local_rewrite_copy(rewrite_mode, rewrite_copy_without_api):
            client = require_client()
            if client is None:
                return
        progress = st.progress(0)
        new_results = []
        for idx, url in enumerate(urls, start=1):
            scraped = None
            try:
                scraped = scrape_product_url(url)
                scraped = enrich_scraped_with_downloaded_images(scraped, should_download=download_images)
            except Exception as exc:
                log_scraping_error(url, exc)
                st.warning(f"Błąd dla {url}: {safe_str(exc)}")
                progress.progress(idx / max(len(urls), 1))
                continue
            payload = build_generation_payload(scraped, category=category, producer=producer, keywords=keywords, features=features)
            try:
                generated = generate_with_claude(
                    client,
                    model,
                    rewrite_mode,
                    payload,
                    rewrite_copy_without_api=rewrite_copy_without_api,
                )
                new_results.append(
                    build_common_result(
                        scraped,
                        generated,
                        category,
                        producer,
                        discount,
                        gauge=gauge,
                        availability=availability,
                        delivery=delivery,
                        use_full_name_as_product_code=use_full_name_as_product_code,
                    )
                )
            except Exception as exc:
                log_generation_error(url, exc)
                st.warning(f"Błąd generowania dla {url}: {safe_str(exc)}")
            progress.progress(idx / max(len(urls), 1))
        append_results(new_results)
        st.success(f"Gotowe. Dodano {len(new_results)} wyników.")


def tab_manual(model, rewrite_mode, rewrite_copy_without_api):
    st.subheader("Wpisz ręcznie")
    gauge_options = ["", "Towar", "Pojazd", "Pojazd dla dzieci"]
    availability_options = ["", "Auto"]
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
        extra1, extra2, extra3 = st.columns(3)
        with extra1:
            gauge = st.selectbox("Gauge", gauge_options, key="manual_gauge")
        with extra2:
            availability = st.selectbox("Availability", availability_options, key="manual_availability")
        with extra3:
            delivery = st.text_input("Delivery (liczba dni)", key="manual_delivery")
        use_full_name_as_product_code = st.checkbox(
            "Użyj pełnej nazwy produktu jako product_code",
            value=False,
            key="manual_product_code_full_name",
        )
        uploaded_image = st.file_uploader("Zdjęcie JPG/PNG (opcjonalne)", type=["jpg", "jpeg", "png"])
        submit = st.form_submit_button("Generuj opis", use_container_width=True)
    if submit:
        client = None
        if not should_use_local_rewrite_copy(rewrite_mode, rewrite_copy_without_api):
            client = require_client()
            if client is None:
                return
        scraped = {"url": "manual", "title": name, "sku": sku, "price": parse_float(price), "weight": parse_float(weight), "available": True, "images": [], "page_text": existing_description, "existing_description": existing_description}
        payload = {"name": name, "category": category, "producer": producer, "sku": sku, "price": price, "weight": weight, "keywords": keywords, "features": features, "existing_description": existing_description}
        try:
            generated = generate_with_claude(
                client,
                model,
                rewrite_mode,
                payload,
                uploaded_image=uploaded_image,
                rewrite_copy_without_api=rewrite_copy_without_api,
            )
        except Exception as exc:
            log_generation_error("manual", exc)
            st.error(f"Błąd generowania: {safe_str(exc)}")
            return
        append_results([
            build_common_result(
                scraped,
                generated,
                category,
                producer,
                discount,
                gauge=gauge,
                availability=availability,
                delivery=delivery,
                use_full_name_as_product_code=use_full_name_as_product_code,
            )
        ])
        st.success("Dodano wynik ręczny.")


def tab_bulk(model, rewrite_mode, rewrite_copy_without_api):
    st.subheader("Bulk (kategoria)")
    gauge_options = ["", "Towar", "Pojazd", "Pojazd dla dzieci"]
    availability_options = ["", "Auto"]
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
    extra1, extra2, extra3 = st.columns(3)
    with extra1:
        gauge = st.selectbox("Gauge", gauge_options, key="bulk_gauge")
    with extra2:
        availability = st.selectbox("Availability", availability_options, key="bulk_availability")
    with extra3:
        delivery = st.text_input("Delivery (liczba dni)", key="bulk_delivery")
    download_images = st.checkbox(
        "Pobierz zdjęcia produktu (JPG/PNG)",
        value=False,
        key="bulk_download_images",
    )
    use_full_name_as_product_code = st.checkbox(
        "Użyj pełnej nazwy produktu jako product_code",
        value=False,
        key="bulk_product_code_full_name",
    )
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
            client = None
            if not should_use_local_rewrite_copy(rewrite_mode, rewrite_copy_without_api):
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
                    scraped = enrich_scraped_with_downloaded_images(scraped, should_download=download_images)
                except Exception as exc:
                    log_scraping_error(preview.get("url"), exc)
                    st.warning(f"Błąd scrapingu dla {preview.get('url')}: {safe_str(exc)}")
                    progress.progress((idx + 1) / max(len(chosen), 1))
                    continue
                sku = scraped.get("sku") or (f"{sku_prefix}-{str(int(start_number)+idx).zfill(3)}" if sku_prefix else "")
                payload = build_generation_payload(scraped, category=category, producer=producer, keywords=keywords, features="", sku_override=sku)
                try:
                    generated = generate_with_claude(
                        client,
                        model,
                        rewrite_mode,
                        payload,
                        rewrite_copy_without_api=rewrite_copy_without_api,
                    )
                    scraped['sku'] = sku
                    new_results.append(
                        build_common_result(
                            scraped,
                            generated,
                            category,
                            producer,
                            discount,
                            gauge=gauge,
                            availability=availability,
                            delivery=delivery,
                            use_full_name_as_product_code=use_full_name_as_product_code,
                        )
                    )
                except Exception as exc:
                    log_generation_error(preview.get("url"), exc)
                    st.warning(f"Błąd generowania dla {preview.get('url')}: {safe_str(exc)}")
                progress.progress((idx + 1) / max(len(chosen), 1))
            append_results(new_results)
            st.success(f"Zakończono generację dla {len(new_results)} produktów.")


def render_results():
    st.markdown("---")
    st.header("Wyniki")
    gauge_options = ["", "Towar", "Pojazd", "Pojazd dla dzieci"]
    availability_options = ["", "Auto"]
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
            gauge_current = item.get("gauge", "")
            availability_current = item.get("availability", "")
            delivery_current = item.get("delivery", "")
            delivery_match = re.search(r"(\d+)", safe_str(delivery_current))
            delivery_input_value = delivery_match.group(1) if delivery_match else ""
            extra1, extra2, extra3 = st.columns(3)
            with extra1:
                item["gauge"] = st.selectbox("Gauge", gauge_options, index=gauge_options.index(gauge_current) if gauge_current in gauge_options else 0, key=f"gauge_{suffix}")
            with extra2:
                item["availability"] = st.selectbox("Availability", availability_options, index=availability_options.index(availability_current) if availability_current in availability_options else 0, key=f"availability_{suffix}")
            with extra3:
                delivery_input = st.text_input("Delivery (liczba dni)", value=delivery_input_value, key=f"delivery_{suffix}")
                item["delivery"] = normalize_delivery_days(delivery_input)
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
            with st.expander("Pola vinted-bulk-uploader", expanded=False):
                v1, v2 = st.columns(2)
                with v1:
                    item["vinted_external_id"] = st.text_input(
                        "external_id",
                        value=item.get("vinted_external_id") or default_vinted_external_id(idx),
                        key=f"vinted_external_id_{suffix}",
                    )
                    current_mode = item.get("vinted_description_mode", "ai")
                    item["vinted_description_mode"] = st.selectbox(
                        "description_mode",
                        VINTED_DESCRIPTION_MODE_OPTIONS,
                        index=VINTED_DESCRIPTION_MODE_OPTIONS.index(current_mode) if current_mode in VINTED_DESCRIPTION_MODE_OPTIONS else 2,
                        key=f"vinted_description_mode_{suffix}",
                    )
                    current_group = item.get("vinted_category_group", "")
                    item["vinted_category_group"] = st.selectbox(
                        "category_group",
                        VINTED_CATEGORY_GROUP_OPTIONS,
                        index=VINTED_CATEGORY_GROUP_OPTIONS.index(current_group) if current_group in VINTED_CATEGORY_GROUP_OPTIONS else 0,
                        key=f"vinted_category_group_{suffix}",
                    )
                    item["vinted_category_type"] = st.text_input(
                        "category_type",
                        value=item.get("vinted_category_type", ""),
                        key=f"vinted_category_type_{suffix}",
                    )
                    item["vinted_brand"] = st.text_input(
                        "brand",
                        value=item.get("vinted_brand", "") or item.get("producer", "") or item.get("brand", ""),
                        key=f"vinted_brand_{suffix}",
                    )
                    item["vinted_size"] = st.text_input(
                        "size",
                        value=item.get("vinted_size", ""),
                        key=f"vinted_size_{suffix}",
                    )
                with v2:
                    current_condition = item.get("vinted_condition", "")
                    item["vinted_condition"] = st.selectbox(
                        "condition",
                        VINTED_CONDITION_OPTIONS,
                        index=VINTED_CONDITION_OPTIONS.index(current_condition) if current_condition in VINTED_CONDITION_OPTIONS else 0,
                        key=f"vinted_condition_{suffix}",
                    )
                    current_color = item.get("vinted_color", "")
                    item["vinted_color"] = st.selectbox(
                        "color",
                        VINTED_COLOR_OPTIONS,
                        index=VINTED_COLOR_OPTIONS.index(current_color) if current_color in VINTED_COLOR_OPTIONS else 0,
                        key=f"vinted_color_{suffix}",
                    )
                    item["vinted_material"] = st.text_input(
                        "material",
                        value=item.get("vinted_material", ""),
                        key=f"vinted_material_{suffix}",
                    )
                    item["vinted_currency"] = st.text_input(
                        "currency",
                        value=item.get("vinted_currency", "PLN") or "PLN",
                        key=f"vinted_currency_{suffix}",
                    )
                    current_package = item.get("vinted_package_size", "")
                    item["vinted_package_size"] = st.selectbox(
                        "package_size",
                        VINTED_PACKAGE_SIZE_OPTIONS,
                        index=VINTED_PACKAGE_SIZE_OPTIONS.index(current_package) if current_package in VINTED_PACKAGE_SIZE_OPTIONS else 0,
                        key=f"vinted_package_size_{suffix}",
                    )
                    item["vinted_publish"] = st.checkbox(
                        "publish",
                        value=bool(item.get("vinted_publish", True)),
                        key=f"vinted_publish_{suffix}",
                    )
                photo_preview = pick_vinted_photo_values(item)
                if photo_preview:
                    st.caption("photo_1-4:")
                    st.code("\n".join(photo_preview[:4]), language="text")
            if st.session_state.debug_mode:
                st.caption(
                    f"Diag kategoryzacji: method={item.get('category_method','')} | "
                    f"reason={item.get('category_reason','')} | "
                    f"confidence={float(parse_float(item.get('category_confidence')) or 0.0):.2f} | "
                    f"score={'' if item.get('category_score') is None else item.get('category_score')}"
                )
                if item.get("model_debug"):
                    st.caption("Diag modelu:")
                    st.json(item.get("model_debug"))
                if item.get("scrape_debug"):
                    st.caption("Diag scrapingu:")
                    scrape_debug_view = dict(item.get("scrape_debug") or {})
                    scrape_debug_view["final_product_code"] = item.get("product_code", "")
                    st.json(scrape_debug_view)
                if item.get("category_signals"):
                    st.json(item.get("category_signals"))
            images = item.get("images") or []
            if images and st.checkbox("Pokaż pierwsze zdjęcie", value=False, key=f"show_img_{suffix}"):
                st.image(images[0], caption="Pierwsze zdjęcie produktu", use_container_width=True)
            downloaded_images = item.get("downloaded_images") or []
            if downloaded_images:
                st.caption(f"Pobrane pliki JPG/PNG: {len(downloaded_images)}")
                if item.get("downloaded_images_dir"):
                    st.code(item.get("downloaded_images_dir", ""), language="text")
    st.session_state.results = results
    csv_data = export_csv_bytes(results)
    vinted_rows, vinted_errors = build_vinted_export_rows(results)
    if st.session_state.debug_mode:
        with st.expander("Preview eksportu CSV", expanded=False):
            preview_rows = to_shoper_rows(results[:1])
            st.caption(f"Liczba kolumn CSV: {len(EXPORT_HEADERS)}")
            st.caption(f"Ostatnie 4 kolumny: {', '.join(EXPORT_HEADERS[-4:])}")
            if preview_rows:
                preview_fields = [
                    "product_code",
                    "vat",
                    "unit",
                    "category",
                    "producer",
                    "weight",
                    "active",
                    "name",
                    "short_description",
                    "description",
                    "price",
                    "stock",
                    "seo_title",
                    "seo_description",
                    "seo_url",
                    "price_buying",
                    "gauge",
                    "availability",
                    "delivery",
                ]
                st.json({field: preview_rows[0].get(field, "") for field in preview_fields})
            else:
                st.caption("Brak rekordów do podglądu eksportu.")
    top_download_slot.download_button("Pobierz CSV Shoper", data=csv_data, file_name="generator-chatshoper-export.csv", mime="text/csv", use_container_width=True, key="download_top")
    st.download_button("Pobierz CSV Shoper", data=csv_data, file_name="generator-chatshoper-export.csv", mime="text/csv", use_container_width=True, key="download_bottom")
    with st.expander("Eksport vinted-bulk-uploader", expanded=False):
        st.caption(f"Liczba kolumn: {len(VINTED_BULK_HEADERS)}")
        st.caption(f"Kolejność nagłówków: {', '.join(VINTED_BULK_HEADERS)}")
        st.caption("Pola wymagane: external_id, title, description_mode, category_group, category_type, brand, size, condition, color, price, currency, photo_1, publish")
        st.caption("Pola opcjonalne: description, material, photo_2, photo_3, photo_4, package_size, sku")
        if vinted_rows:
            st.json({field: vinted_rows[0].get(field, "") for field in VINTED_BULK_HEADERS})
        if vinted_errors:
            st.warning("Eksport vinted-bulk-uploader jest zablokowany do czasu uzupełnienia wymaganych pól.")
            for error in vinted_errors[:50]:
                st.write(f"- {error}")
        else:
            vinted_csv_data, _ = export_vinted_bulk_csv_bytes(results)
            st.download_button(
                "Pobierz CSV vinted-bulk-uploader",
                data=vinted_csv_data,
                file_name="generator-chatshoper-vinted-bulk-uploader.csv",
                mime="text/csv",
                use_container_width=True,
                key="download_vinted_bulk",
            )


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
    st.metric("Trace Olek/Cloudflare", len(st.session_state.olek_browser_trace))
    if st.button("Wyczyść logi debug", key="clear_debug_logs"):
        st.session_state.scraping_errors = []
        st.session_state.generation_errors = []
        st.session_state.olek_browser_trace = []
        st.success("Wyczyszczono logi debug.")
    with st.expander("Log błędów scrapingu", expanded=False):
        if not st.session_state.scraping_errors:
            st.caption("Brak błędów scrapingu.")
        for entry in reversed(st.session_state.scraping_errors[-50:]):
            st.write(f"{entry.get('time','')} | {entry.get('url','')} | {entry.get('error','')}")
            scrape_meta = {
                "stage": entry.get("stage"),
                "status_code": entry.get("status_code"),
                "source_domain": entry.get("source_domain"),
                "final_url": entry.get("final_url"),
                "response_title": entry.get("response_title"),
                "waf_detected": entry.get("waf_detected"),
                "waf_vendor": entry.get("waf_vendor"),
                "selectors_tried": entry.get("selectors_tried"),
                "json_ld_items_count": entry.get("json_ld_items_count"),
                "candidate_anchor_count": entry.get("candidate_anchor_count"),
            }
            if any(value not in ("", None, False, []) for value in scrape_meta.values()):
                st.json(scrape_meta)
            if entry.get("response_preview"):
                st.caption("Response preview:")
                st.code(entry.get("response_preview", "")[:2000], language="html")
    with st.expander("Log błędów generowania", expanded=False):
        if not st.session_state.generation_errors:
            st.caption("Brak błędów generowania.")
        for entry in reversed(st.session_state.generation_errors[-50:]):
            st.write(f"{entry.get('time','')} | {entry.get('url','')} | {entry.get('error','')}")
            parse_meta = {
                "parsed_json_success": entry.get("parsed_json_success"),
                "json_parse_strategy": entry.get("json_parse_strategy"),
                "json_parse_error": entry.get("json_parse_error"),
                "repair_attempted": entry.get("repair_attempted"),
                "repair_success": entry.get("repair_success"),
            }
            if any(value not in ("", None, False) for value in parse_meta.values()):
                st.json(parse_meta)
            if entry.get("raw_model_response_preview"):
                st.caption("Raw model response preview:")
                st.code(entry.get("raw_model_response_preview", "")[:2000], language="text")
            if entry.get("repair_response_preview"):
                st.caption("Repair response preview:")
                st.code(entry.get("repair_response_preview", "")[:2000], language="text")
    with st.expander("Trace Olek / Cloudflare", expanded=True):
        if not st.session_state.olek_browser_trace:
            st.caption("Brak zdarzeń Olek / Cloudflare.")
        for entry in reversed(st.session_state.olek_browser_trace[-120:]):
            st.write(f"{entry.get('time','')} | {entry.get('event','')}")
            entry_view = dict(entry)
            entry_view.pop("time", None)
            entry_view.pop("event", None)
            if entry_view:
                st.json(entry_view)


def main():
    st.set_page_config(page_title="generator-chatshoper", layout="wide")
    init_state()
    render_css()
    model, rewrite_mode, rewrite_copy_without_api = render_sidebar()
    render_intro()
    tab1, tab2, tab3 = st.tabs(["Z linków produktów", "Wpisz ręcznie", "Bulk (kategoria)"])
    with tab1:
        tab_links(model, rewrite_mode, rewrite_copy_without_api)
    with tab2:
        tab_manual(model, rewrite_mode, rewrite_copy_without_api)
    with tab3:
        tab_bulk(model, rewrite_mode, rewrite_copy_without_api)
    render_results()
    render_debug_panel()


if __name__ == "__main__":
    main()
