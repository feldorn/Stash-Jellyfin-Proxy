"""Image generation and transformation helpers.

Pillow is optional — if not installed the functions return a fallback
placeholder PNG (initialized lazily on first call). Every function that
does image work returns (bytes, content_type) and swallows Pillow errors
back to a safe placeholder so clients never see a 5xx here.
"""
import io
import logging
import os
from typing import Tuple

try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover — only hit on minimal installs
    PILLOW_AVAILABLE = False

logger = logging.getLogger("stash-jellyfin-proxy")

# Minimal 1x1 dark PNG used as last-resort fallback before lazy init runs.
_FALLBACK_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc'
    b'\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)

_PLACEHOLDER_PNG = None


def placeholder_png() -> bytes:
    """Return the 400x600 dark-blue placeholder PNG, rendering it on
    first call. Falls back to a 1x1 minimum PNG if Pillow is missing or
    rendering errors."""
    global _PLACEHOLDER_PNG
    if _PLACEHOLDER_PNG is not None:
        return _PLACEHOLDER_PNG
    if PILLOW_AVAILABLE:
        try:
            img = Image.new('RGB', (400, 600), (26, 26, 46))
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            _PLACEHOLDER_PNG = buf.getvalue()
            return _PLACEHOLDER_PNG
        except Exception:
            pass
    _PLACEHOLDER_PNG = _FALLBACK_PNG
    return _PLACEHOLDER_PNG


def crop_to_portrait(image_data: bytes, target_width: int = 400, target_height: int = 600,
                     anchor: str = "center") -> Tuple[bytes, str]:
    """Crop a source image to a 2:3 portrait using cover+crop (no letterbox).
    Source is scaled to fill the target frame on the narrower dimension,
    then cropped horizontally (for landscape sources) or vertically (for
    portrait sources) using the configured anchor.

    anchor: "center" (default), "left", "right" for horizontal crop;
            "top", "bottom" for vertical crop (ignored when source is
            already wider than target).

    Returns (image_bytes, content_type). Pillow failure → return source
    bytes untouched so the caller still has a usable image."""
    if not PILLOW_AVAILABLE:
        return image_data, "image/jpeg"

    try:
        img = Image.open(io.BytesIO(image_data))
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (20, 20, 20))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        src_w, src_h = img.size
        target_ratio = target_width / target_height  # 2/3 ≈ 0.667
        src_ratio = src_w / src_h

        if src_ratio > target_ratio:
            # Source is wider than target — scale to match height, crop sides.
            scale = target_height / src_h
            new_w = int(src_w * scale)
            new_h = target_height
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            if anchor == "left":
                x0 = 0
            elif anchor == "right":
                x0 = new_w - target_width
            else:
                x0 = (new_w - target_width) // 2
            img = img.crop((x0, 0, x0 + target_width, target_height))
        else:
            # Source is taller than target — scale to match width, crop top/bottom.
            scale = target_width / src_w
            new_w = target_width
            new_h = int(src_h * scale)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            if anchor == "top":
                y0 = 0
            elif anchor == "bottom":
                y0 = new_h - target_height
            else:
                y0 = (new_h - target_height) // 2
            img = img.crop((0, y0, target_width, y0 + target_height))

        output = io.BytesIO()
        img.save(output, format='JPEG', quality=85)
        return output.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning(f"Portrait crop failed: {e}, returning original")
        return image_data, "image/jpeg"


def pad_image_to_portrait(image_data: bytes, target_width: int = 400, target_height: int = 600) -> Tuple[bytes, str]:
    """Pad a source image to a portrait 2:3 canvas using contain+pad.
    Returns (image_bytes, content_type). Pillow failure → return the
    source bytes untouched so the caller still has a usable image."""
    if not PILLOW_AVAILABLE:
        return image_data, "image/jpeg"

    try:
        img = Image.open(io.BytesIO(image_data))
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (20, 20, 20))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        width, height = img.size
        scale_w = target_width / width
        scale_h = target_height / height
        scale = min(scale_w, scale_h)

        new_width = int(width * scale)
        new_height = int(height * scale)

        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        canvas = Image.new('RGB', (target_width, target_height), (20, 20, 20))
        x_offset = (target_width - new_width) // 2
        y_offset = (target_height - new_height) // 2
        canvas.paste(img, (x_offset, y_offset))

        output = io.BytesIO()
        canvas.save(output, format='JPEG', quality=85)
        return output.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning(f"Image padding failed: {e}, returning original")
        return image_data, "image/jpeg"


_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _find_font_path():
    for p in _FONT_PATHS:
        if os.path.exists(p):
            return p
    return None


def _draw_centered_label(img, text: str, max_chars_per_line: int = 16,
                         max_lines: int = 4, text_color=(74, 144, 217)) -> None:
    """Draw `text` word-wrapped + centered on a PIL Image in-place.

    Word-wraps by character count, auto-shrinks the font from 48px down
    to 24px until every line fits within the image minus a 30px margin,
    and vertically centers the resulting block. Shared by
    `generate_text_icon` (draws on a blank canvas) and
    `compose_library_card` (draws on a darkened screenshot)."""
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    width, height = img.size

    PADDING = 30
    max_text_width = width - (PADDING * 2)
    font_path_found = _find_font_path()

    # Word-wrap using character count as a rough guide.
    words = text.split()
    lines = []
    current_line = ""
    for word in words:
        test_line = (current_line + " " + word).strip() if current_line else word
        if len(test_line) <= max_chars_per_line:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            if len(word) > max_chars_per_line:
                current_line = word[:max_chars_per_line - 3] + "..."
            else:
                current_line = word
    if current_line:
        lines.append(current_line)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > max_chars_per_line - 3:
            lines[-1] = lines[-1][:max_chars_per_line - 3] + "..."
        else:
            lines[-1] = lines[-1] + "..."

    font_size = 48
    min_font_size = 24
    font = None
    while font_size >= min_font_size:
        if font_path_found:
            try:
                font = ImageFont.truetype(font_path_found, font_size)
            except (IOError, OSError):
                font = ImageFont.load_default()
                break
        else:
            font = ImageFont.load_default()
            break
        all_fit = True
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            if line_width > max_text_width:
                all_fit = False
                break
        if all_fit:
            break
        font_size -= 2
    if font is None:
        font = ImageFont.load_default()

    logger.debug(f"Label '{text}': {len(lines)} lines, font size {font_size}px")

    line_heights = []
    line_widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])

    line_spacing = 10
    total_height = sum(line_heights) + (len(lines) - 1) * line_spacing if lines else 0
    start_y = (height - total_height) // 2

    current_y = start_y
    for i, line in enumerate(lines):
        x = (width - line_widths[i]) // 2
        draw.text((x, current_y), line, fill=text_color, font=font)
        current_y += line_heights[i] + line_spacing


def generate_text_icon(text: str, width: int = 400, height: int = 600,
                       max_chars_per_line: int = 16, max_lines: int = 4) -> Tuple[bytes, str]:
    """Generate a portrait 2:3 PNG icon with word-wrapped text label on a
    dark-navy background. Pillow failure → placeholder_png()."""
    if not PILLOW_AVAILABLE:
        logger.debug("Pillow not available, returning placeholder PNG")
        return placeholder_png(), "image/png"

    try:
        img = Image.new('RGB', (width, height), (26, 26, 46))
        _draw_centered_label(img, text, max_chars_per_line, max_lines)
        output = io.BytesIO()
        img.save(output, format='PNG')
        return output.getvalue(), "image/png"
    except Exception as e:
        logger.warning(f"Text icon generation failed: {e}")
        return placeholder_png(), "image/png"


def compose_library_card(image_bytes: bytes, label: str,
                         width: int = 400, height: int = 600,
                         dim_factor: float = 0.5) -> Tuple[bytes, str]:
    """Turn a raw scene screenshot into a library-tile card: crop to 2:3
    portrait, darken uniformly, and overlay `label` using the same
    word-wrap + auto-fit + centered-draw pipeline as `generate_text_icon`.

    `dim_factor` is the multiplier applied to every pixel — 0.5 means
    50% darker (roughly half-brightness). Lower = darker backdrop =
    more legible text.

    On any Pillow failure returns a text-only card as a safe fallback
    so the client still gets a labelled tile."""
    if not PILLOW_AVAILABLE:
        return generate_text_icon(label, width, height, max_chars_per_line=12, max_lines=4)

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGB', img.size, (20, 20, 20))
            if img.mode == 'P':
                img = img.convert('RGBA')
            bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # Cover-crop to target portrait (reuse crop_to_portrait's math
        # inline to avoid a round-trip through JPEG encode/decode).
        src_w, src_h = img.size
        target_ratio = width / height
        src_ratio = src_w / src_h
        if src_ratio > target_ratio:
            scale = height / src_h
            new_w = int(src_w * scale)
            img = img.resize((new_w, height), Image.Resampling.LANCZOS)
            x0 = (new_w - width) // 2
            img = img.crop((x0, 0, x0 + width, height))
        else:
            scale = width / src_w
            new_h = int(src_h * scale)
            img = img.resize((width, new_h), Image.Resampling.LANCZOS)
            y0 = (new_h - height) // 2
            img = img.crop((0, y0, width, y0 + height))

        # Uniform darken. Image.point scales every channel by dim_factor.
        dim_factor = max(0.0, min(1.0, float(dim_factor)))
        img = img.point(lambda p: int(p * dim_factor))

        # Overlay the library label centered on top.
        _draw_centered_label(img, label, max_chars_per_line=12, max_lines=4)

        output = io.BytesIO()
        img.save(output, format='PNG')
        return output.getvalue(), "image/png"
    except Exception as e:
        logger.warning(f"compose_library_card failed for '{label}': {e}, falling back to text icon")
        return generate_text_icon(label, width, height, max_chars_per_line=12, max_lines=4)


_MENU_ICON_LABELS = {
    "root-scenes": "Scenes",
    "root-studios": "Studios",
    "root-performers": "Performers",
    "root-groups": "Groups",
    "root-series": "Series",
    "root-tag": "Tags",
    "root-tags": "Tags",
}


def menu_icon_label(icon_type: str) -> str:
    """Resolve the display label for a root-* library id. Falls back to
    a titlecased version of the suffix."""
    return _MENU_ICON_LABELS.get(
        icon_type,
        icon_type.replace("root-", "").replace("-", " ").title(),
    )


def generate_menu_icon(icon_type: str, width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Top-level folder icon — 12 chars wide, 4 lines max. Fallback when
    no scene screenshot is available; the compose_library_card path is
    preferred whenever the scene screenshot fetch succeeds."""
    return generate_text_icon(menu_icon_label(icon_type), width, height,
                              max_chars_per_line=12, max_lines=4)


def generate_filter_icon(text: str, width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Filter folder icon — 10 chars wide, 6 lines max for poster display."""
    return generate_text_icon(text, width, height, max_chars_per_line=10, max_lines=6)


def generate_placeholder_icon(item_type: str = "group", width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Placeholder icon for items without Stash art."""
    if not PILLOW_AVAILABLE:
        return placeholder_png(), "image/png"

    try:
        from PIL import ImageDraw
        img = Image.new('RGB', (width, height), (30, 30, 35))
        draw = ImageDraw.Draw(img)
        placeholder_color = (80, 80, 90)

        if item_type == "group":
            draw.rectangle([120, 200, 280, 360], outline=placeholder_color, width=6)
            for y in [220, 270, 320]:
                draw.rectangle([130, y, 150, y+20], fill=placeholder_color)
                draw.rectangle([250, y, 270, y+20], fill=placeholder_color)
        else:
            draw.ellipse([140, 200, 260, 320], outline=placeholder_color, width=6)
            draw.text((180, 230), "?", fill=placeholder_color)

        output = io.BytesIO()
        img.save(output, format='PNG')
        return output.getvalue(), "image/png"
    except Exception as e:
        logger.warning(f"Placeholder icon generation failed: {e}")
        return _FALLBACK_PNG, "image/png"
