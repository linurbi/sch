"""Read the EAN-13 printed on a catalog product card.

The catalogs mix two kinds of barcode artwork, and neither is readable by a
single method:

* Vector barcodes, which rasterise cleanly and are read by zxing.
* Low resolution bitmaps (about 1.1 pixels per module), where anti-aliased
  bars defeat zxing, but averaging ink coverage per module recovers the bits.

Every result is validated by the guard patterns, an exact match against the
EAN-13 digit alphabet and the check digit. Where two methods both produce a
value they must agree, otherwise the card is reported as unreadable so the
importer can fall back to the SCH item number.
"""

from __future__ import annotations

import cv2
import numpy as np
import pymupdf
import zxingcpp

L_CODES = [
    "0001101", "0011001", "0010011", "0111101", "0100011",
    "0110001", "0101111", "0111011", "0110111", "0001011",
]
G_CODES = [code[::-1].translate(str.maketrans("01", "10")) for code in L_CODES]
R_CODES = [code.translate(str.maketrans("01", "10")) for code in L_CODES]
PARITY_TO_FIRST_DIGIT = {
    "LLLLLL": 0, "LLGLGG": 1, "LLGGLG": 2, "LLGGGL": 3, "LGLLGG": 4,
    "LGGLLG": 5, "LGGGLL": 6, "LGLGLG": 7, "LGLGGL": 8, "LGGLGL": 9,
}
LEFT_ALPHABET = [(digit, "L", code) for digit, code in enumerate(L_CODES)]
LEFT_ALPHABET += [(digit, "G", code) for digit, code in enumerate(G_CODES)]
RIGHT_ALPHABET = [(digit, "R", code) for digit, code in enumerate(R_CODES)]

# Offsets from the SKU/packing block to the barcode strip of the same card.
BAND_TOP = 120
BAND_BOTTOM = 200


def check_digit_ok(code: str) -> bool:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(code[:12]))
    return (10 - total % 10) % 10 == int(code[12])


MIN_MARGIN = 0.3


def match_digit(chunk: np.ndarray, alphabet) -> tuple[int, str] | None:
    """Identify a digit from its seven module levels. Clean artwork matches a
    pattern outright; blurred low resolution bars fall back to the closest
    pattern, but only when the runner-up is clearly worse."""
    bits = "".join("1" if value > 0.5 else "0" for value in chunk)
    exact = next((item for item in alphabet if item[2] == bits), None)
    if exact is not None:
        return exact[0], exact[1]
    scores = []
    for digit, kind, code in alphabet:
        expected = np.array([float(bit) for bit in code])
        scores.append((float(((chunk - expected) ** 2).sum()), digit, kind))
    scores.sort()
    if scores[0][0] > 1.0 or scores[1][0] - scores[0][0] < MIN_MARGIN:
        return None
    return scores[0][1], scores[0][2]


def decode_levels(levels: np.ndarray) -> str | None:
    bits = "".join("1" if level > 0.5 else "0" for level in levels)
    if bits[:3] != "101" or bits[45:50] != "01010" or bits[92:95] != "101":
        return None
    digits: list[int] = []
    parity: list[str] = []
    for index in range(6):
        match = match_digit(levels[3 + index * 7 : 10 + index * 7], LEFT_ALPHABET)
        if match is None:
            return None
        digits.append(match[0])
        parity.append(match[1])
    for index in range(6):
        match = match_digit(levels[50 + index * 7 : 57 + index * 7], RIGHT_ALPHABET)
        if match is None:
            return None
        digits.append(match[0])
    first = PARITY_TO_FIRST_DIGIT.get("".join(parity))
    if first is None:
        return None
    code = str(first) + "".join(str(digit) for digit in digits)
    return code if check_digit_ok(code) else None


def module_levels(profile: np.ndarray, left: float, right: float) -> np.ndarray | None:
    """Ink coverage per module, scaled to 0 (white) .. 1 (solid bar)."""
    ink = 255.0 - profile.astype(float)
    edges = np.linspace(left, right + 1, 96)
    values = []
    for index in range(95):
        start, end = edges[index], edges[index + 1]
        lo, hi = int(np.floor(start)), int(np.ceil(end))
        span = np.arange(lo, hi)
        weights = np.clip(
            np.minimum(span + 1, end) - np.maximum(span, start), 0, None
        )
        segment = ink[lo:hi]
        if segment.size == 0 or weights.sum() <= 0:
            return None
        values.append(float((segment * weights).sum() / weights.sum()))
    series = np.array(values)
    spread = series.max() - series.min()
    if spread <= 1:
        return None
    return (series - series.min()) / spread


def decode_raster(document: pymupdf.Document, xref: int) -> str | None:
    try:
        pix = pymupdf.Pixmap(document, xref)
    except (ValueError, RuntimeError):
        return None
    if pix.n > 1:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    gray = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
    # Digits sit under the bars, so measure the upper part of the artwork only.
    bars = gray[: max(2, int(gray.shape[0] * 0.55))]
    profile = bars.mean(axis=0)
    dark = bars.min(axis=0) < 200
    if not dark.any():
        return None
    left = int(np.argmax(dark))
    right = len(dark) - int(np.argmax(dark[::-1])) - 1
    found = set()
    for start in range(max(0, left - 3), left + 4):
        for end in range(right - 3, min(gray.shape[1], right + 4)):
            if end - start < 80:
                continue
            levels = module_levels(profile, start, end)
            code = decode_levels(levels) if levels is not None else None
            if code:
                found.add(code)
    return found.pop() if len(found) == 1 else None


def dark_runs(dark: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = None
    for index, flag in enumerate(dark):
        if flag and start is None:
            start = index
        if not flag and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(dark) - 1))
    return runs


def bar_spans(dark: np.ndarray) -> list[tuple[int, int]]:
    """Cluster bars so a kosher badge next to the barcode is excluded."""
    runs = dark_runs(dark)
    if not runs:
        return []
    module = min(end - start + 1 for start, end in runs)
    gap_limit = max(6, module * 7)
    groups: list[list[tuple[int, int]]] = []
    current = [runs[0]]
    for run in runs[1:]:
        if run[0] - current[-1][1] - 1 <= gap_limit:
            current.append(run)
        else:
            groups.append(current)
            current = [run]
    groups.append(current)
    spans = [(group[0][0], group[-1][1]) for group in groups if len(group) >= 20]
    return sorted(spans, key=lambda span: span[1] - span[0], reverse=True)


def bar_band(gray: np.ndarray) -> tuple[int, int] | None:
    dark = (gray < 128).astype(np.uint8)
    transitions = np.abs(np.diff(dark, axis=1)).sum(axis=1)
    fraction = dark.mean(axis=1)
    mask = (transitions > 1500) & (fraction < 0.5)
    runs: list[tuple[int, int]] = []
    start = None
    for index, flag in enumerate(mask):
        if flag and start is None:
            start = index
        if not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    if not runs:
        return None
    return max(runs, key=lambda run: run[1] - run[0])


def decode_scanline_grid(gray: np.ndarray) -> str | None:
    band = bar_band(gray)
    if band is None:
        return None
    top, bottom = band
    margin = max(4, int(gray.shape[1] * 0.04))
    for ratio in (0.3, 0.45, 0.6):
        row = int(top + (bottom - top) * ratio)
        if not 0 <= row < gray.shape[0]:
            continue
        dark = gray[row] < 128
        dark[:margin] = False
        dark[-margin:] = False
        if not dark.any():
            continue
        for start, end in bar_spans(dark):
            levels = module_levels(gray[row], start, end)
            code = decode_levels(levels) if levels is not None else None
            if code:
                return code
    return None


def decode_zxing(gray: np.ndarray) -> str | None:
    padded = cv2.copyMakeBorder(gray, 40, 40, 80, 80, cv2.BORDER_CONSTANT, value=255)
    for result in zxingcpp.read_barcodes(
        padded,
        formats=[zxingcpp.BarcodeFormat.EAN13],
        try_rotate=True,
        try_downscale=True,
        try_invert=True,
    ):
        if result.valid and result.text.isdigit() and len(result.text) == 13:
            return result.text
    return None


def clip_gray(page: pymupdf.Page, anchor: tuple, dpi: int) -> np.ndarray:
    x0, y0, x1, _y1 = anchor[:4]
    pix = page.get_pixmap(
        clip=pymupdf.Rect(x0 - 6, y0 + BAND_TOP, x1 + 6, y0 + BAND_BOTTOM),
        dpi=dpi,
        alpha=False,
        colorspace=pymupdf.csGRAY,
    )
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)


def barcode_images(page: pymupdf.Page, anchor: tuple) -> list[int]:
    x0, y0, x1, _y1 = anchor[:4]
    found = []
    for info in page.get_image_info(xrefs=True):
        bbox = info["bbox"]
        inside = (
            y0 + 100 < bbox[1]
            and bbox[3] < y0 + 215
            and x0 - 20 < bbox[0]
            and bbox[2] < x1 + 20
        )
        wide = info["width"] >= 90 and info["width"] / max(info["height"], 1) >= 1.4
        # Inline images report xref 0 and cannot be extracted by reference.
        if inside and wide and info["xref"] > 0:
            found.append(info["xref"])
    return found


def read_card_ean(
    document: pymupdf.Document, page: pymupdf.Page, anchor: tuple
) -> str | None:
    candidates: set[str] = set()
    for xref in barcode_images(page, anchor):
        code = decode_raster(document, xref)
        if code:
            candidates.add(code)
    if not candidates:
        for dpi in (600, 2400):
            gray = clip_gray(page, anchor, dpi)
            for code in (decode_zxing(gray), decode_scanline_grid(gray)):
                if code:
                    candidates.add(code)
            if candidates:
                break
    # Conflicting reads mean the artwork is ambiguous; report nothing.
    return candidates.pop() if len(candidates) == 1 else None
