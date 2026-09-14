"""Re-apply name cleanup to an existing products.json without re-running OCR."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from catalog_text import clean_name, scrub_ocr_name

PRODUCTS_PATH = Path(__file__).resolve().parents[1] / "data" / "products.json"


def main() -> int:
    data = json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))
    changed = 0
    for product in data["products"]:
        tidy = scrub_ocr_name(clean_name(product["name"], product.get("brand", "")), product.get("brand", ""))
        if tidy and tidy != product["name"]:
            print(f"  {product['name']}  ->  {tidy}")
            product["name"] = tidy
            changed += 1
    PRODUCTS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nTidied {changed} of {len(data['products'])} names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
