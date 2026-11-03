"""Conservative table candidates from pixel geometry, without OCR/model boxes.

Coordinates are half-open source-pixel rectangles, as accepted by Pillow.crop.
No rotation/rescaling, topic counts, course names or model-provided ROI is used.
A verified monochrome grid returns the entire image instead of an invented
tight box; ambiguous unclosed final headers otherwise return no candidate. A candidate may contain any number of printed rows.
"""
from __future__ import annotations
import io
import math
from collections import Counter
from statistics import median
from PIL import Image, ImageChops, UnidentifiedImageError


def _bands(image):
    width, height = image.size
    hsv = image.convert('HSV')
    saturation = hsv.getchannel('S').point([255 if n >= 64 else 0 for n in range(256)])
    brightness = hsv.getchannel('V').point([255 if n >= 40 else 0 for n in range(256)])
    mask = ImageChops.multiply(saturation, brightness).tobytes()
    pixels = hsv.tobytes()
    required = math.ceil(width * .30)
    rows = []
    for y in range(height):
        row = mask[y * width:(y + 1) * width]
        if row.count(255) >= required:
            rows.append((y, row.find(b'\xff'), row.rfind(b'\xff') + 1))
    groups = []
    gap = max(2, min(4, height // 800))
    for row in rows:
        if not groups or row[0] - groups[-1][-1][0] > gap + 1:
            groups.append([])
        groups[-1].append(row)
    result = []
    for group in groups:
        y0, y1 = group[0][0], group[-1][0] + 1
        if y1 - y0 < max(4, height // 400):
            continue
        x0 = int(median(row[1] for row in group))
        x1 = int(median(row[2] for row in group))
        hues = Counter()
        for y in range(y0, y1, max(1, (y1 - y0) // 8)):
            for x in range(x0, x1, max(1, (x1 - x0) // 256)):
                offset = y * width + x
                if mask[offset]:
                    hues[pixels[offset * 3]] += 1
        if hues:
            result.append({'box': [x0, y0, x1, y1], 'hue': hues.most_common(1)[0][0]})
    return result


def _footer(header, band):
    hx0, hy0, hx1, hy1 = header['box']
    x0, y0, x1, y1 = band['box']
    overlap = max(0, min(hx1, x1) - max(hx0, x0))
    aligned = overlap >= .85 * max(hx1 - hx0, x1 - x0)
    hue_difference = abs(header['hue'] - band['hue'])
    hue_difference = min(hue_difference, 256 - hue_difference)
    return (aligned and y1 - y0 < .85 * (hy1 - hy0)
            and hue_difference >= 18 and y0 - hy1 >= max(8, min(24, hy1 - hy0)))


def _padded(box, width, height):
    x0, y0, x1, y1 = box
    return [max(0, x0 - 2), max(0, y0 - 2), min(width, x1 + 2), min(height, y1 + 2)]


def _verified_black_grid(image):
    """A black grid licenses keeping the whole image, never a tight free crop."""
    width, height = image.size
    data = image.convert('L').point([255 if n <= 90 else 0 for n in range(256)]).tobytes()
    rows = []
    for y in range(height):
        row = data[y * width:(y + 1) * width]
        if row.count(255) >= math.ceil(width * .30):
            rows.append((y, row.find(b'\xff'), row.rfind(b'\xff') + 1))
    grouped = []
    for row in rows:
        if not grouped or row[0] > grouped[-1][-1][0] + 1:
            grouped.append([])
        grouped[-1].append(row)
    lines = [(int(median(r[1] for r in group)), group[0][0],
              int(median(r[2] for r in group)), group[-1][0] + 1)
             for group in grouped if group[-1][0] - group[0][0] + 1 <= max(3, height // 200)]
    tolerance = max(3, width // 50)
    for first in lines:
        aligned = [line for line in lines if abs(line[0] - first[0]) <= tolerance and abs(line[2] - first[2]) <= tolerance]
        if len(aligned) < 3:
            continue
        x0 = int(median(line[0] for line in aligned)); x1 = int(median(line[2] for line in aligned))
        y0 = aligned[0][1]; y1 = aligned[-1][3]
        if y1 - y0 < max(24, height // 10):
            continue
        def vertical(x):
            return sum(any(data[y * width + column] for column in range(max(0, x - 2), min(width, x + 3)))
                       for y in range(y0, y1)) >= .8 * (y1 - y0)
        if not vertical(x0) or not vertical(x1 - 1):
            continue
        # At least one independent divider, in addition to both outer borders.
        if any(vertical(x) for x in range(x0 + 6, x1 - 6)):
            return True
    return False


def detect_table_regions(image_bytes):
    """Return [{id, bboxpixel, header_bbox, method}], never a guessed free box.

    Wide saturated bands cover at least 30% of the original image width.
    A shorter, differently colored aligned band closes the preceding table.
    Otherwise a following header is the explicit upper boundary of that ROI.
    An unclosed last band is omitted. A verified black grid permits only a
    full-image fallback, with no inferred header or cropped content boundary.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > 25_000_000 or min(source.size) < 16:
                return []
            if source.mode in ('RGBA', 'LA') or 'transparency' in source.info:
                rgba = source.convert('RGBA')
                image = Image.new('RGBA', source.size, 'white')
                image.alpha_composite(rgba)
                image = image.convert('RGB')
            else:
                image = source.convert('RGB')
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return []
    width, height = image.size
    bands = _bands(image)
    regions = []
    index = 0
    while index < len(bands):
        header = bands[index]
        footers = []
        following = index + 1
        while following < len(bands) and _footer(header, bands[following]):
            footers.append(bands[following])
            following += 1
        hx0, hy0, hx1, hy1 = header['box']
        if footers:
            end = footers[-1]['box'][3]
            box = _padded([min(hx0, footers[-1]['box'][0]), hy0,
                           max(hx1, footers[-1]['box'][2]), end], width, height)
            method = 'saturation_wide_header_color_footer'
        elif following < len(bands):
            end = bands[following]['box'][1]
            if end - hy1 < max(12, hy1 - hy0):
                index += 1
                continue
            box = _padded([hx0, hy0, hx1, end], width, height)
            box[3] = min(box[3], end)  # never include any next-header pixels
            method = 'saturation_wide_header_next_header'
        else:
            break
        regions.append({'id': f'table-{len(regions) + 1}', 'bboxpixel': box,
                        'header_bbox': _padded(header['box'], width, height), 'method': method})
        index = following
    if not regions and _verified_black_grid(image):
        return [{'id': 'table-1', 'bboxpixel': [0, 0, width, height],
                 'header_bbox': None, 'method': 'full_image_verified_black_grid'}]
    return regions
