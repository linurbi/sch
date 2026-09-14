"""Build the product picker data from the original SCH PDF catalogs.

The PDF text layer contains the exact position of every product label and SKU.
That lets us crop the matching product card without relying on the unrelated
reading order used by the flipbook HTML.

Merge policy is intentionally asymmetric:
1. Import every unique SKU from sch2025.pdf.
2. Import only SKUs not already present from sch2026.pdf.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image
import zxingcpp

from catalog_text import BRAND_WORDS, clean_name, normalize_space


ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "data" / "pdf"
IMAGE_DIR = ROOT / "data" / "pdf-images"
PRODUCTS_PATH = ROOT / "data" / "products.json"

PDF_SOURCES = (
    ("2025", PDF_DIR / "sch2025.pdf"),
    ("2026", PDF_DIR / "sch2026.pdf"),
)

# SKU/packing text is a separate positioned block above every product card.
SKU_BLOCK_RE = re.compile(r"^(?P<sku>\d{5,6})\s+(?:\d+\s*-|1(?:\s|$))")
STAND_RE = re.compile(r"(?:סטנד|דאמפ)")
EAN_FORMATS = (zxingcpp.BarcodeFormat.EAN13, zxingcpp.BarcodeFormat.EAN8)


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


def block_text(block: tuple) -> str:
    return normalize_space(block[4])


def page_header(page: pymupdf.Page) -> str:
    """Read the vertical brand title at the outside edge of a spread."""
    candidates: list[tuple[float, str]] = []
    for block in page.get_text("blocks"):
        x0, y0, x1, y1 = block[:4]
        text = block_text(block)
        if (
            x0 >= page.rect.width - 65
            and y0 < 300
            and y1 - y0 > 25
            and not re.search(r"\d", text)
        ):
            candidates.append((y1 - y0, text))
    if not candidates:
        return ""
    return max(candidates)[1]


def brand_for_name(name: str, header: str) -> str:
    normalized = clean_name(name, "")
    for candidate in sorted(BRAND_WORDS, key=len, reverse=True):
        if re.search(rf"(?<!\S){re.escape(candidate)}(?!\S)", normalized):
            return candidate
    return clean_name(header.split("//")[0], "")


def collapse_duplicate_text(text: str) -> str:
    """Remove a PDF text layer phrase duplicated exactly on top of itself."""
    words = text.split()
    if len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            return " ".join(words[:half])
    return text


def name_for_anchor(page: pymupdf.Page, anchor: tuple, header: str) -> str:
    x0, y0, x1, _y1 = anchor[:4]
    # Across both supplied PDFs, product labels are 165-220 points below
    # their SKU/packing block and share its horizontal card boundaries.
    area = pymupdf.Rect(x0 - 5, y0 + 165, x1 + 5, y0 + 220)
    text = collapse_duplicate_text(normalize_space(page.get_textbox(area)))
    return clean_name(text, header.split("//")[0].strip())


def pixmap_bgr(page: pymupdf.Page, scale: float) -> np.ndarray:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n
    )
    if pix.n == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
    return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)


def decode_ean(render: np.ndarray, anchor: tuple, scale: float) -> str | None:
    x0, y0, x1, _y1 = anchor[:4]
    crop = render[
        int((y0 + 70) * scale) : int((y0 + 180) * scale),
        max(0, int((x0 - 6) * scale)) : int((x1 + 6) * scale),
    ]
    if crop.size == 0:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    variants = (
        gray,
        cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
    )
    for image in variants:
        results = zxingcpp.read_barcodes(
            image,
            formats=EAN_FORMATS,
            try_rotate=True,
            try_downscale=True,
            try_invert=True,
        )
        for result in results:
            if result.valid and result.text.isdigit():
                return result.text
    return None


def save_card(render: np.ndarray, anchor: tuple, scale: float, sku: str) -> str:
    x0, y0, x1, _y1 = anchor[:4]
    crop = render[
        max(0, int((y0 - 20) * scale)) : int((y0 + 220) * scale),
        max(0, int((x0 - 2) * scale)) : int((x1 + 2) * scale),
    ]
    rel = f"data/pdf-images/{sku}.jpg"
    if crop.size:
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        Image.fromarray(rgb).save(ROOT / rel, quality=85, optimize=True)
    return rel


def anchors_for_page(page: pymupdf.Page) -> list[tuple[str, tuple]]:
    found: list[tuple[str, tuple]] = []
    seen: set[str] = set()
    for block in page.get_text("blocks"):
        match = SKU_BLOCK_RE.match(block_text(block))
        if not match:
            continue
        sku = match.group("sku")
        if sku in seen:
            continue
        seen.add(sku)
        found.append((sku, block))
    return found


def import_pdf(
    catalog: str,
    path: Path,
    known_skus: set[str],
    known_eans: set[str],
    known_names: set[str],
) -> list[Product]:
    document = pymupdf.open(path)
    imported: list[Product] = []
    render_scale = 3.0

    print(f"\n=== {path.name}: {len(document)} PDF pages ===")
    for page_index, page in enumerate(document):
        anchors = anchors_for_page(page)
        pending = [(sku, anchor) for sku, anchor in anchors if sku not in known_skus]
        if not pending:
            continue

        header = page_header(page)
        candidates: list[tuple[str, tuple, str, str]] = []
        for sku, anchor in pending:
            name = name_for_anchor(page, anchor, header)
            if not name or STAND_RE.search(name):
                continue
            brand = brand_for_name(name, header)
            if not brand:
                continue
            candidates.append((sku, anchor, name, brand))
        if not candidates:
            continue

        render = pixmap_bgr(page, render_scale)
        ean_count = 0
        for sku, anchor, name, brand in candidates:
            ean = decode_ean(render, anchor, render_scale)
            name_key = normalize_space(name).casefold()
            # A product's SCH SKU can change between catalogs. Treat the EAN
            # or an exact normalized product name as the same base product too.
            if (ean and ean in known_eans) or name_key in known_names:
                known_skus.add(sku)
                continue
            if ean:
                ean_count += 1
            image = save_card(render, anchor, render_scale, sku)
            imported.append(
                Product(
                    name=name,
                    barcode=ean or sku,
                    sku=sku,
                    brand=brand,
                    catalog=catalog,
                    page=page_index + 1,
                    image=image,
                    barcode_source="ean" if ean else "sku",
                )
            )
            known_skus.add(sku)
            known_names.add(name_key)
            if ean:
                known_eans.add(ean)

        print(
            f"  page {page_index + 1:02d}: "
            f"{len(candidates)} products, {ean_count} EANs, {header}",
            flush=True,
        )
    return imported


def main() -> int:
    missing = [str(path) for _catalog, path in PDF_SOURCES if not path.exists()]
    if missing:
        print("Missing PDF files:\n  " + "\n  ".join(missing))
        return 1

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the directory itself: a local web server can hold a Windows handle
    # to it even though its generated files are safe to replace.
    for old_image in IMAGE_DIR.glob("*.jpg"):
        try:
            old_image.unlink()
        except PermissionError:
            pass

    known_skus: set[str] = set()
    known_eans: set[str] = set()
    known_names: set[str] = set()
    products: list[Product] = []
    source_counts: dict[str, int] = {}
    for catalog, path in PDF_SOURCES:
        added = import_pdf(catalog, path, known_skus, known_eans, known_names)
        products.extend(added)
        source_counts[catalog] = len(added)

    products.sort(key=lambda product: (product.brand, product.name, product.sku))
    payload = {
        "catalogs": [path.name for _catalog, path in PDF_SOURCES],
        "merge_policy": "all products from sch2025.pdf, then SKU delta from sch2026.pdf",
        "source_counts": source_counts,
        "count": len(products),
        "products": [asdict(product) for product in products],
    }
    PRODUCTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    eans = sum(product.barcode_source == "ean" for product in products)
    print(
        f"\nWrote {len(products)} products ({eans} EAN, "
        f"{len(products) - eans} SKU fallback) to {PRODUCTS_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
