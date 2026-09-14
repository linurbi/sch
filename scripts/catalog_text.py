"""Product name cleanup shared by the catalog importer and the tidy script."""

from __future__ import annotations

import re

# Every brand that appears in a catalog table of contents, including the second
# half of shared spreads such as "ג'ונסונס / הירו". A spread header leaks into the
# first product name, so knowing the brand words lets us strip it back off.
BRAND_WORDS = {
    "אג'קס", "אדם", "או בה", "או.בה", "אוטלי", "אילי", "אלמקס", "ארם אנד ארמור",
    "בטיסט", "בלונס", "בלונס בייבי", "בלקוני", "ברילה", "ג'ונסונס", "דריזלישס",
    "הירו", "וניה", "טודיי", "לוקיטוס", "לנדוור", "לרו", "ליידי ספיד סטיק",
    "מאסטר שף", "מנה", "מקס ברנר", "מרבה", "מריטו", "מרידול", "ניוטרוג'ינה",
    "סטרימר", "סלימדליס", "ספיד סטיק", "פוף", "פולרטי", "פומפדור", "פלמוליב",
    "פרי", "צ'וקטה", "צ'וקטה סולטי", "צ'וקטה קידס", "קולגייט", "קוקי",
    "קראנצ'וס", "קרפרי", "ריו מרה", "שיק", "שיק אינטואישן",
}

BRANDS_BY_LENGTH = sorted(BRAND_WORDS, key=len, reverse=True)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_quotes(text: str) -> str:
    """Catalog text mixes geresh, curly quotes and ASCII quotes for the same word."""
    for src in "\u05f3\u2019\u2018\u00b4":
        text = text.replace(src, "'")
    for src in "\u05f4\u201c\u201d":
        text = text.replace(src, '"')
    return text


def strip_duplicate_brand(name: str) -> str:
    """Drop a leading brand when the product's own brand follows it."""
    for first in BRANDS_BY_LENGTH:
        if not name.startswith(first + " "):
            continue
        rest = name[len(first) + 1 :]
        moved = ""
        gap = re.match(r"^(\S{1,4})\s+", rest)
        if gap and any(rest[gap.end() :].startswith(b) for b in BRAND_WORDS):
            moved = " " + gap.group(1)
            rest = rest[gap.end() :]
        if any(rest == b or rest.startswith(b + " ") for b in BRAND_WORDS):
            return normalize_space(rest + moved)
        break
    return name


def clean_name(name: str, brand: str) -> str:
    name = normalize_quotes(normalize_space(name))
    name = re.sub(r"\s*חדש!+\s*", " ", name)
    # Spread headers such as "אג'קס // פלמוליב" get glued to the first product name.
    name = re.sub(r"^.*?//\s*", "", name)
    if brand:
        brand = normalize_quotes(brand)
        name = re.sub(rf"^(?:{re.escape(brand)}\s*)+", brand + " ", name, flags=re.I)
    name = strip_duplicate_brand(normalize_space(name))
    # A latin or numeric token first makes the whole line flip direction in WhatsApp.
    lead = re.match(r"^([A-Za-z0-9][A-Za-z0-9.+\-/]*)\s+(?=[\u0590-\u05FF])", name)
    if lead:
        name = f"{name[lead.end():]} {lead.group(1)}"
    return re.sub(r"\s+", " ", name).strip(" ,.-|/\\")


def is_ocr_noise(token: str) -> bool:
    """Drop tokens RapidOCR invents on candy packaging photos."""
    t = token.strip()
    if not t or t.startswith("!"):
        return True
    if re.fullmatch(r"[!UTtn]+", t):
        return True
    letters = re.sub(r"[^A-Za-z]", "", t)
    # Keep size codes (S, L, XL, x) and short tokens.
    if len(letters) < 5:
        return False
    if letters and not re.search(r"[aeiouyAEIOUY]", letters):
        return True
    return False


def scrub_ocr_name(name: str, brand: str) -> str:
    parts = [p for p in name.split() if not is_ocr_noise(p)]
    cleaned = clean_name(" ".join(parts), brand)
    return cleaned or brand or name
