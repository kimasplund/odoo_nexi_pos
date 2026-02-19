# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Receipt PNG renderer for NEXI terminal printer.

Renders receipt text and HTML as PNG images suitable for thermal printers.
Uses Pillow (PIL) for text-to-PNG and wkhtmltoimage for HTML-to-PNG.
Both are already available in Odoo's Python environment.
"""

import base64
import io
import logging
import subprocess
import textwrap

from odoo.tools import find_in_path

_logger = logging.getLogger(__name__)

# Thermal printer constants
RECEIPT_WIDTH_PX = 384  # 80mm paper at 203 DPI
FONT_SIZE = 16
LINE_HEIGHT = 20
MARGIN_X = 8
MARGIN_Y = 8

# Max base64 length per NEXI API fragment
MAX_FRAGMENT_LENGTH = 262143


def _get_monospace_font(size=FONT_SIZE):
    """Get a monospace font, falling back gracefully.

    Returns:
        PIL ImageFont instance
    """
    from PIL import ImageFont

    # Try common monospace fonts in order of preference
    font_paths = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
        '/usr/share/fonts/TTF/DejaVuSansMono.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf',
        '/usr/share/fonts/truetype/freefont/FreeMono.ttf',
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue

    _logger.warning("No monospace TTF font found, using PIL default")
    return ImageFont.load_default()


def render_text_receipt(text, width=RECEIPT_WIDTH_PX):
    """Render receipt text as a monospace PNG for thermal printer.

    Args:
        text: Receipt text content (plain text with newlines)
        width: Image width in pixels (default 384 for 80mm paper)

    Returns:
        base64-encoded PNG string
    """
    from PIL import Image, ImageDraw

    if not text:
        return ''

    font = _get_monospace_font()

    # Calculate character width for wrapping
    # Use getbbox for Pillow 10+ compatibility
    char_bbox = font.getbbox('M')
    char_width = char_bbox[2] - char_bbox[0]
    usable_width = width - (2 * MARGIN_X)
    chars_per_line = max(1, usable_width // char_width)

    # Wrap and split lines
    lines = []
    for raw_line in text.split('\n'):
        if not raw_line.strip():
            lines.append('')
        else:
            wrapped = textwrap.wrap(raw_line, width=chars_per_line) or ['']
            lines.extend(wrapped)

    # Calculate image height
    img_height = (2 * MARGIN_Y) + (len(lines) * LINE_HEIGHT)

    # Create image
    img = Image.new('RGB', (width, img_height), 'white')
    draw = ImageDraw.Draw(img)

    # Draw text
    y = MARGIN_Y
    for line in lines:
        draw.text((MARGIN_X, y), line, fill='black', font=font)
        y += LINE_HEIGHT

    # Encode to base64 PNG
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode('utf-8')


def render_html_receipt(html_content, width=RECEIPT_WIDTH_PX):
    """Render HTML receipt as PNG using wkhtmltoimage.

    Uses Odoo's wkhtmltopdf installation (wkhtmltoimage is included).

    Args:
        html_content: HTML string to render
        width: Image width in pixels (default 384)

    Returns:
        base64-encoded PNG string
    """
    if not html_content:
        return ''

    try:
        wkhtmltoimage = find_in_path('wkhtmltoimage')
    except IOError:
        _logger.error("wkhtmltoimage not found; falling back to text rendering")
        # Strip HTML tags as crude fallback
        import re
        text = re.sub(r'<[^>]+>', '', html_content)
        return render_text_receipt(text, width)

    cmd = [
        wkhtmltoimage,
        '--width', str(width),
        '--format', 'png',
        '--quality', '100',
        '--disable-smart-width',
        '-', '-',  # stdin -> stdout
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=html_content.encode('utf-8'),
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0:
            _logger.error("wkhtmltoimage failed (code %s): %s",
                          proc.returncode, proc.stderr.decode('utf-8', errors='replace'))
            return ''

        return base64.b64encode(proc.stdout).decode('utf-8')

    except subprocess.TimeoutExpired:
        _logger.error("wkhtmltoimage timed out")
        return ''
    except Exception:
        _logger.exception("wkhtmltoimage error")
        return ''


def split_fragments(contents_base64):
    """Split base64 content into fragments for the NEXI print API.

    Each fragment must be <= 262,143 characters.
    All but the last fragment are sent with continue=True.

    Args:
        contents_base64: Full base64-encoded PNG string

    Returns:
        List of (chunk, is_continuation) tuples
    """
    if not contents_base64:
        return []

    if len(contents_base64) <= MAX_FRAGMENT_LENGTH:
        return [(contents_base64, False)]

    fragments = []
    for i in range(0, len(contents_base64), MAX_FRAGMENT_LENGTH):
        chunk = contents_base64[i:i + MAX_FRAGMENT_LENGTH]
        is_last = (i + MAX_FRAGMENT_LENGTH) >= len(contents_base64)
        fragments.append((chunk, not is_last))

    return fragments
