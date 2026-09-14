"""Download SCH flipbook catalogs and build products.json + product crops."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path
import cv2
import numpy as np
import requests
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

from catalog_text import BRAND_WORDS, clean_name, is_ocr_noise, normalize_space

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
IMAGE_DIR = DATA_DIR / "images"
CACHE_DIR = ROOT / ".cache"
PRODUCTS_PATH = DATA_DIR / "products.json"

UA = "Mozilla/5.0 (compatible; SCH-catalog-picker/1.0)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})

SKU_PACK_RE = re.compile(
    r"(?P<name>.+?)\s+(?P<sku>\d{5,7})\s+"
    r"(?P<pack>\d{1,3}\s*[-–xX]\s*\d{1,5}(?:\s*[-–xX]\s*\d{1,5}){0,2})",
    re.S,
)
# Two page numbers at the end of a spread, e.g. "28 29" or "47 46 חדש!".
# Require they are consecutive catalog pages so pack sizes like "1248 64" do not match.
PAGE_PAIR_RE = re.compile(
    r"(?<!\d)(\d{1,3})(?!\d)\s+(?<!\d)(\d{1,3})(?!\d)(?:\s+חדש!?)*\s*$"
)
PAGE_SINGLE_RE = re.compile(r"(?<!\d)(\d{1,3})(?!\d)(?:\s+חדש!?)*\s*$")
TOC_HREF_RE = re.compile(r"\./(\d{1,3})-(\d{1,3})/")
DIGITS_RE = re.compile(r"\D+")
SKIP_BRANDS = {"אודות", "עמוד ראשי", "עמוד אחורי", "צור קשר"}


@dataclass
class Product:
    name: str
    barcode: str
    sku: str
    brand: str
    catalog: str
    page: int
    image: str
    barcode_source: str


class ParagraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paragraphs: list[str] = []
        self.toc: list[tuple[int, int, str]] = []
        self._in_p = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "p":
            self._in_p = True
            self._buf = []
        if tag == "a":
            href = attrs_d.get("href", "")
            title = attrs_d.get("title", "").strip()
            m = TOC_HREF_RE.search(href)
            if m and title:
                a, b = int(m.group(1)), int(m.group(2))
                self.toc.append((min(a, b), max(a, b), unescape_basic(title)))

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            text = normalize_space("".join(self._buf))
            if text:
                self.paragraphs.append(text)
            self._in_p = False

    def handle_data(self, data):
        if self._in_p:
            self._buf.append(data)


def unescape_basic(text: str) -> str:
    return (
        text.replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def ean13_valid(code: str) -> bool:
    if len(code) != 13 or not code.isdigit():
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(code[:12]))
    return (10 - total % 10) % 10 == int(code[12])


def ean_candidates(digits: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(code: str) -> None:
        if code not in seen and ean13_valid(code):
            seen.add(code)
            found.append(code)

    if len(digits) >= 13:
        for i in range(len(digits) - 12):
            add(digits[i : i + 13])
    if len(digits) == 12:
        add("8" + digits)
    if len(digits) == 14:
        for i in range(14):
            add(digits[:i] + digits[i + 1 :])
    if len(digits) == 15 and digits[0] in "18":
        add(digits[1:14])
        add(digits[2:])
    return found


def find_ean(texts: list[str]) -> str | None:
    blobs = [DIGITS_RE.sub("", text) for text in texts]
    blobs.append("".join(blobs))
    preferred_prefix = ("87", "80", "72", "88", "40", "50", "69", "84", "80")
    ranked: list[str] = []
    for digits in blobs:
        ranked.extend(ean_candidates(digits))
    if not ranked:
        return None
    ranked.sort(key=lambda code: (0 if code[:2] in preferred_prefix else 1, code))
    return ranked[0]


def http_ok(url: str) -> bool:
    try:
        r = SESSION.head(url, timeout=20, allow_redirects=True)
        if r.status_code == 405:
            r = SESSION.get(url, timeout=20, stream=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def get_bytes(url: str) -> bytes:
    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def probe_catalogs(years: tuple[int, ...] = (26, 25)) -> list[str]:
    found: list[str] = []
    for year in years:
        for month in range(1, 13):
            cat_id = f"{month:02d}{year:02d}"
            url = f"https://www.sch.co.il/catalog{cat_id}/"
            if http_ok(url):
                found.append(cat_id)
                print(f"  found {url}")
    return found


def page_image_url(cat_id: str, page: int, quality: int = 4) -> str:
    return (
        f"https://www.sch.co.il/catalog{cat_id}/"
        f"files/assets/common/page-html5-substrates/page{page:04d}_{quality}.jpg"
    )


def count_pages(cat_id: str) -> int:
    lo, hi = 1, 80
    last = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if http_ok(page_image_url(cat_id, mid)):
            last = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return last


def brand_for_page(toc: list[tuple[int, int, str]], book_page: int) -> str:
    for start, end, title in toc:
        if start <= book_page <= end:
            return title.split("/")[0].strip()
    return ""


def parse_html(html: str) -> tuple[list[str], list[tuple[int, int, str]]]:
    parser = ParagraphParser()
    parser.feed(html)
    return parser.paragraphs, parser.toc


def parse_products(text: str, brand: str) -> list[dict]:
    items: list[dict] = []
    for match in SKU_PACK_RE.finditer(text):
        name = clean_name(match.group("name"), brand)
        sku = match.group("sku")
        if not name or is_stand(name, sku):
            continue
        items.append({"name": name, "sku": sku, "brand": infer_brand(name, brand)})
    return items


def infer_brand(name: str, fallback: str) -> str:
    normalized = clean_name(name, "")
    for candidate in sorted(BRAND_WORDS, key=len, reverse=True):
        if normalized == candidate or normalized.startswith(candidate + " "):
            return candidate
    return fallback.split("/")[0].strip()


def is_stand(name: str, sku: str) -> bool:
    if "סטנד" in name or "דאמפ" in name:
        return True
    return sku.startswith("53") and not any(ch.isalpha() for ch in name)


def detect_footers(bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
    b, g, r = cv2.split(bgr)
    mask = (
        (np.abs(r.astype(int) - 30) < 20)
        & (np.abs(g.astype(int) - 62) < 20)
        & (np.abs(b.astype(int) - 100) < 28)
    ).astype(np.uint8) * 255
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
    )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if 150 < w < 500 and 18 < h < 110:
            boxes.append((x, y, w, h))
    return boxes


def sort_tiles_rtl(
    boxes: list[tuple[int, int, int, int]], page_width: int
) -> list[tuple[int, int, int, int]]:
    mid = page_width / 2

    def key(box: tuple[int, int, int, int]) -> tuple[int, int, int]:
        x, y, w, _h = box
        cx = x + w / 2
        side = 0 if cx >= mid else 1
        return (side, y // 80, -x)

    return sorted(boxes, key=key)


def spread_from_image(image_no: int) -> tuple[int, int]:
    # FlipBuilder image 3 contains printed pages 6–7, image 55 contains
    # pages 110–111, and so on.
    return image_no * 2, image_no * 2 + 1


def ocr_ean_from_tile(bgr: np.ndarray, footer: tuple[int, int, int, int], ocr: RapidOCR) -> str | None:
    x, y, w, _h = footer
    top = max(0, y - 190)
    crop = bgr[top : y + 12, x : x + w]
    if crop.size == 0:
        return None
    tmp = CACHE_DIR / "_ean_crop.jpg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    Image.fromarray(rgb).save(tmp, quality=95)
    result, _ = ocr(str(tmp))
    texts = [item[1] for item in result] if result else []
    return find_ean(texts)


def page_ocr_results(
    image_path: Path, ocr: RapidOCR
) -> list[tuple[float, float, str]]:
    """OCR a spread once and retain each text box's center and text."""
    result, _ = ocr(str(image_path))
    positioned: list[tuple[float, float, str]] = []
    for box, text, _score in result or []:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        positioned.append((sum(xs) / len(xs), sum(ys) / len(ys), text))
    return positioned


def ean_from_page_results(
    positioned: list[tuple[float, float, str]],
    footer: tuple[int, int, int, int],
) -> str | None:
    x, y, w, _h = footer
    texts = [
        text
        for cx, cy, text in positioned
        if x - 20 <= cx <= x + w + 20 and y - 210 <= cy <= y + 12
    ]
    return find_ean(texts)


def save_product_image(bgr: np.ndarray, footer: tuple[int, int, int, int], barcode: str) -> str:
    rel = f"data/images/{barcode}.jpg"
    dest = ROOT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return rel
    x, y, w, h = footer
    top = max(0, y - 520)
    crop = bgr[top : y + h, x : x + w]
    if crop.size:
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        try:
            Image.fromarray(rgb).save(dest, quality=82, optimize=True)
        except OSError:
            # Windows can lock a file the local HTTP server is serving.
            try:
                Image.fromarray(rgb).save(dest, quality=82)
            except OSError:
                pass
    return rel


def pages_from_paragraph(text: str) -> set[int]:
    pair = PAGE_PAIR_RE.search(text)
    if pair:
        a, b = int(pair.group(1)), int(pair.group(2))
        if 1 <= a <= 150 and 1 <= b <= 150 and abs(a - b) == 1:
            return {a, b}
    single = PAGE_SINGLE_RE.search(text)
    if single:
        n = int(single.group(1))
        if 1 <= n <= 150:
            other = n + 1 if n % 2 == 0 else n - 1
            if 1 <= other <= 150:
                return {n, other}
    return set()


def paragraph_for_image(
    paragraphs: list[str], image_no: int
) -> str | None:
    left, right = spread_from_image(image_no)
    for text in paragraphs:
        pages = pages_from_paragraph(text)
        if left in pages or right in pages:
            return text
    return None


def spread_paragraph(cat_id: str, image_no: int, fallback: list[str]) -> str | None:
    """Read the spread's own HTML, which includes text omitted by the homepage."""
    left, right = spread_from_image(image_no)
    route = f"{right}-{left}"
    url = f"https://www.sch.co.il/catalog{cat_id}/{route}/"
    try:
        html = get_bytes(url).decode("utf-8", "replace")
        paragraphs, _toc = parse_html(html)
        # Individual spread pages contain one product paragraph.
        candidates = [p for p in paragraphs if SKU_PACK_RE.search(p)]
        if candidates:
            return max(candidates, key=len)
    except requests.RequestException:
        pass
    return paragraph_for_image(fallback, image_no)


def name_from_ocr_texts(texts: list[str], brand: str) -> str:
    """Build a short label from OCR on the product photo when HTML has no name."""
    words: list[str] = []
    seen: set[str] = set()
    for raw in texts:
        token = raw.strip()
        if not token or find_ean([token]):
            continue
        if is_ocr_noise(token) or re.fullmatch(r"[\d.\-%\"']+", token):
            continue
        if len(token) < 2:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(token)
    if not words:
        return brand or "מוצר"
    composed = clean_name(" ".join(words[:6]), brand)
    if brand and brand not in composed and not composed.startswith(brand):
        composed = f"{brand} {composed}"
    return composed


def ocr_tile_texts(
    bgr: np.ndarray, footer: tuple[int, int, int, int], ocr: RapidOCR
) -> list[str]:
    x, y, w, h = footer
    top = max(0, y - 480)
    crop = bgr[top : y + h, x : x + w]
    if crop.size == 0:
        return []
    tmp = CACHE_DIR / "_tile_ocr.jpg"
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    Image.fromarray(rgb).save(tmp, quality=92)
    result, _ = ocr(str(tmp))
    return [item[1] for item in result] if result else []


def process_catalog(cat_id: str, ocr: RapidOCR) -> list[Product]:
    print(f"\n=== catalog{cat_id} ===")
    html = get_bytes(f"https://www.sch.co.il/catalog{cat_id}/").decode("utf-8", "replace")
    paragraphs, toc = parse_html(html)
    pages = count_pages(cat_id)
    print(f"  {pages} spread images, {len(paragraphs)} text blocks")
    products: list[Product] = []

    for image_no in range(1, pages + 1):
        left, right = spread_from_image(image_no)
        brand = brand_for_page(toc, right) or brand_for_page(toc, left)
        if brand in SKIP_BRANDS:
            continue
        paragraph = spread_paragraph(cat_id, image_no, paragraphs)
        named = parse_products(paragraph, brand) if paragraph else []

        cache_path = CACHE_DIR / cat_id / f"page{image_no:04d}.jpg"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists():
            url = page_image_url(cat_id, image_no)
            try:
                cache_path.write_bytes(get_bytes(url))
            except requests.RequestException as exc:
                print(f"  skip image {image_no}: {exc}")
                continue

        bgr = cv2.imread(str(cache_path))
        if bgr is None:
            continue
        footers = sort_tiles_rtl(detect_footers(bgr), bgr.shape[1])
        if not footers:
            continue

        positioned = page_ocr_results(cache_path, ocr)
        decoded = [
            (footer, ean_from_page_results(positioned, footer))
            for footer in footers
        ]

        usable = [(box, ean) for box, ean in decoded if ean]
        print(
            f"  page {image_no:02d} ({left}-{right}) "
            f"tiles={len(footers)} ean={len(usable)} names={len(named)} {brand}",
            flush=True,
        )

        used_barcodes: set[str] = set()
        # Pair against every detected tile, including a tile whose barcode OCR
        # failed. Removing failed tiles first shifts every subsequent name.
        for (footer, ean), info in zip(decoded, named):
            if not ean or ean in used_barcodes:
                continue
            used_barcodes.add(ean)
            image = save_product_image(bgr, footer, ean)
            products.append(
                Product(
                    name=info["name"],
                    barcode=ean,
                    sku=info["sku"],
                    brand=info["brand"] or brand,
                    catalog=cat_id,
                    page=right,
                    image=image,
                    barcode_source="ean",
                )
            )

        leftovers = [(box, ean) for box, ean in usable if ean and ean not in used_barcodes]
        for footer, ean in leftovers:
            used_barcodes.add(ean)
            image = save_product_image(bgr, footer, ean)
            ocr_name = name_from_ocr_texts(ocr_tile_texts(bgr, footer, ocr), brand)
            products.append(
                Product(
                    name=ocr_name,
                    barcode=ean,
                    sku="",
                    brand=brand,
                    catalog=cat_id,
                    page=right,
                    image=image,
                    barcode_source="ean",
                )
            )

    return products


def name_quality(product: Product) -> int:
    name = (product.name or "").strip()
    brand = (product.brand or "").strip()
    if not name or name == brand:
        return 0
    return len(name)


def apply_known_brand_rules(product: Product) -> Product:
    """Correct shared spreads using manufacturer barcode prefixes."""
    if product.barcode.startswith(("800226", "860226")):
        product.brand = "פולרטי"
        product.name = "פולרטי חטיפי קרח ללא גלוטן"
    elif product.barcode.startswith("57900005"):
        product.brand = "דריזלישס"
        product.name = "דריזלישס מיני פריכיות אורז"
    elif product.barcode.startswith("72901129"):
        product.brand = "מקס ברנר"
    # Barcode confirmed from the catalog artwork supplied by the user.
    if product.barcode == "8718951722507":
        product.brand = "פלמוליב"
        product.name = 'פלמוליב סבון נוזלי חלב שקדים מ"ל 750'
    return product


def merge_products(all_products: list[Product]) -> list[Product]:
    by_barcode: dict[str, Product] = {}
    for product in all_products:
        current = by_barcode.get(product.barcode)
        if current is None:
            by_barcode[product.barcode] = product
            continue
        newer = product.catalog >= current.catalog
        better_name = name_quality(product) > name_quality(current)
        if newer:
            if not better_name and name_quality(current):
                product.name = current.name
                product.sku = product.sku or current.sku
            by_barcode[product.barcode] = product
        elif better_name:
            current.name = product.name
            current.sku = current.sku or product.sku
    items = [apply_known_brand_rules(item) for item in by_barcode.values()]
    items.sort(key=lambda p: (p.brand, p.name, p.barcode))
    return items


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("Probing catalogs...")
    catalogs = probe_catalogs()
    if not catalogs:
        print("No catalogs found.")
        return 1

    print("Loading OCR...")
    ocr = RapidOCR()
    collected: list[Product] = []
    for cat_id in catalogs:
        collected.extend(process_catalog(cat_id, ocr))

    merged = merge_products(collected)
    payload = {
        "catalogs": catalogs,
        "count": len(merged),
        "products": [asdict(p) for p in merged],
    }
    PRODUCTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {len(merged)} products to {PRODUCTS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
