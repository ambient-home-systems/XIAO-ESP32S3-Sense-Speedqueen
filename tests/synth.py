"""Build synthetic panel frames, plus the calibration file the tool would emit.

The frames are crude — flat discs and rectangles on a dark field — but they
carry the colours measured off photographs of the real panels, which is the
part that matters. The TR7's numbers in particular encode the fact that its
lit indicators are DARKER than its unlit ones in the red channel, so a test
frame will reproduce the inversion that makes the channel choice matter.

Nothing here is a substitute for a real photograph. It exercises the decode
path's arithmetic, not the optics.
"""
import io, json, math
from PIL import Image, ImageDraw

PANEL = (8, 8, 12)
ANCHOR = (240, 240, 240)

# Per-machine colours. The TR7 numbers are the medians measured off a real
# photograph of the panel: lit indicators are saturated blue, unlit ones are
# neutral grey and BRIGHTER than the lit ones in red, and the display's
# segments sit on a lit blue backlight field.
PALETTE = {
    "dr7": {"channel": "r",
            "led_lit": (235, 70, 40), "led_unlit": (26, 12, 10),
            "seg_lit": (235, 70, 40), "seg_unlit": (26, 12, 10)},
    "tr7": {"channel": "b",
            "led_lit": (60, 57, 243), "led_unlit": (133, 133, 133),
            "seg_lit": (32, 17, 197), "seg_unlit": (20, 15, 117)},
}

CHAN = {"r": 0, "g": 1, "b": 2}


def level(rgb, channel):
    if channel in CHAN:
        return rgb[CHAN[channel]]
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def midpoint(a, b, channel):
    return int(round((level(a, channel) + level(b, channel)) / 2))

GLYPH_BITS = {
    "0": "1111110", "1": "0110000", "2": "1101101", "3": "1111001",
    "4": "0110011", "5": "1011011", "6": "1011111", "7": "1110000",
    "8": "1111111", "9": "1111011", " ": "0000000", "-": "0000001",
    "n": "0010101", "H": "0110111", "A": "1110111", "F": "1000111",
    "d": "0111101", "L": "0001110",
}
SEGS = ("a", "b", "c", "d", "e", "f", "g")
W, H = 820, 430
ANCHORS = [("tri_up", 600, 120), ("tri_down", 600, 300)]
DIGIT_BOXES = [(648, 160, 60, 104), (722, 160, 60, 104)]


def seg_rects(box):
    """Same geometry the calibration tool derives from a digit box."""
    x, y, w, h = box
    t = max(3, min(w * 0.24, h * 0.13))
    half = h / 2 - t * 0.85
    m = {
        "a": [x + t * 0.8, y, w - t * 1.6, t],
        "f": [x, y + t * 0.8, t, half],
        "b": [x + w - t, y + t * 0.8, t, half],
        "g": [x + t * 0.8, y + h / 2 - t / 2, w - t * 1.6, t],
        "e": [x, y + h / 2 + t * 0.4, t, half],
        "c": [x + w - t, y + h / 2 + t * 0.4, t, half],
        "d": [x + t * 0.8, y + h - t, w - t * 1.6, t],
    }
    return {k: [round(v) for v in r] for k, r in m.items()}


def led_grid(n):
    """n indicator positions laid out on the left of the panel."""
    out = []
    for i in range(n):
        col, row = divmod(i, 9)
        out.append((44 + col * 110, 46 + row * 40))
    return out


class Warp:
    """Mimics a nudged camera: rotate/scale/translate every drawn point."""
    def __init__(self, dx=0.0, dy=0.0, deg=0.0, scale=1.0, cx=410.0, cy=215.0):
        self.dx, self.dy, self.scale = dx, dy, scale
        self.c, self.s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        self.cx, self.cy = cx, cy

    def pt(self, x, y):
        px, py = (x - self.cx) * self.scale, (y - self.cy) * self.scale
        return (self.cx + px * self.c - py * self.s + self.dx,
                self.cy + px * self.s + py * self.c + self.dy)


def render(lit_names, text, leds, radius=7, warp=None, machine="dr7"):
    pal = PALETTE[machine]
    warp = warp or Warp()
    img = Image.new("RGB", (W, H), PANEL)
    d = ImageDraw.Draw(img)

    # The TR7's digits sit on a lit backlight field, so unlit segments are not
    # black — they are the field colour.
    if machine == "tr7":
        bx0, by0 = DIGIT_BOXES[0][0] - 16, DIGIT_BOXES[0][1] - 14
        bx1 = DIGIT_BOXES[-1][0] + DIGIT_BOXES[-1][2] + 16
        by1 = DIGIT_BOXES[-1][1] + DIGIT_BOXES[-1][3] + 14
        d.polygon([warp.pt(bx0, by0), warp.pt(bx1, by0),
                   warp.pt(bx1, by1), warp.pt(bx0, by1)], fill=pal["seg_unlit"])

    for name, (x, y) in leds.items():
        cx, cy = warp.pt(x, y)
        r = radius * warp.scale
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  fill=pal["led_lit"] if name in lit_names else pal["led_unlit"])

    for (_, ax, ay) in ANCHORS:
        p = [warp.pt(ax - 11, ay + 9), warp.pt(ax + 11, ay + 9), warp.pt(ax, ay - 11)]
        d.polygon(p, fill=ANCHOR)

    for i, box in enumerate(DIGIT_BOXES):
        ch = text[i] if i < len(text) else " "
        bits = GLYPH_BITS.get(ch, "0000000")
        rects = seg_rects(box)
        for seg, on in zip(SEGS, bits):
            x, y, w, h = rects[seg]
            corners = [warp.pt(x, y), warp.pt(x + w, y),
                       warp.pt(x + w, y + h), warp.pt(x, y + h)]
            d.polygon(corners, fill=pal["seg_lit"] if on == "1" else pal["seg_unlit"])

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def calibration(machine, led_defs, radius=7, channel=None):
    """What the tool exports: ROI coords, thresholds, labels, machine id.

    Thresholds land midway between the lit and unlit levels in whichever
    channel is being sampled, which is what the tool's gap-finder does.
    """
    pal = PALETTE[machine]
    channel = channel or pal["channel"]
    led_t = midpoint(pal["led_lit"], pal["led_unlit"], channel)
    seg_t = midpoint(pal["seg_lit"], pal["seg_unlit"], channel)
    leds = []
    for group, name, label, (x, y) in led_defs:
        leds.append({"name": name, "group": group, "label": label,
                     "x": x, "y": y, "threshold": led_t})
    digits = []
    for i, box in enumerate(DIGIT_BOXES):
        digits.append({"index": i, "segments": {
            k: {"rect": r, "threshold": seg_t}
            for k, r in seg_rects(box).items()}})
    return {
        "version": 1, "machine": machine, "image_size": [W, H],
        "channel": channel, "led_radius": radius,
        "anchors": [{"name": n, "x": x, "y": y} for n, x, y in ANCHORS],
        "leds": leds, "digits": digits, "needs_more_frames": [],
    }
