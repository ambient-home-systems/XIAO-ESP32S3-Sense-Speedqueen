"""Speed Queen 7-series control panel reader.

Pulls a JPEG snapshot per machine, samples fixed regions of interest, and
publishes the result to Home Assistant over MQTT discovery.

Nothing here is learned or probabilistic. Every indicator is a brightness
threshold over a small disc; every digit is seven thresholds looked up in a
table. The adaptive parts are a two-point similarity transform that corrects
for the camera being nudged, a search that finds a panel the camera moved away
from, and two fixed rules for a digit that reads as no glyph: one segment
sitting on its cut may be read the other way (resolve_digit), and a display
that changed unexpectedly is read again before it is believed
(Reader._confirm).

One add-on instance drives any number of machines. Everything machine-specific
lives in PROFILES; the sampling, transform and glyph machinery is shared
because every 7-series control uses the same two-digit seven-segment display
and the same printed triangles beside it.
"""

import http.server
import io
import json
import logging
import math
import os
import re
import signal
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import paho.mqtt.client as mqtt
from PIL import Image, ImageDraw

DISCOVERY = "homeassistant"

# The calibration tool is served here over Home Assistant ingress. Keep in
# step with `ingress_port` in config.yaml — Supervisor connects to that port
# and nothing else answers on it.
INGRESS_PORT = 8099
TOOL_PATH = "/sq-calibrate.html"

# A calibration file is JSON and small; anything near this is not one.
MAX_UPLOAD = 4 * 1024 * 1024

# Single last-will topic for the whole add-on. Every entity also carries its
# own machine's availability topic and availability_mode "all", so the process
# dying takes every machine offline while one dead camera takes only its own.
ADDON_AVAIL_TOPIC = "speedqueen/panel/availability"

SEG_ORDER = ("a", "b", "c", "d", "e", "f", "g")

# Seven-segment patterns in a,b,c,d,e,f,g order. Digits first, then the letter
# forms the 7-series controls use for diagnostic codes.
GLYPHS = {
    "1111110": "0", "0110000": "1", "1101101": "2", "1111001": "3",
    "0110011": "4", "1011011": "5", "1011111": "6", "1110000": "7",
    "1111111": "8", "1111011": "9",
    "0000000": " ", "0000001": "-",
    "1110111": "A", "1001110": "C", "0111101": "d", "1001111": "E",
    "1000111": "F", "0110111": "H", "0001110": "L", "0010101": "n",
    "0011101": "o", "1100111": "P", "0001111": "t", "0111110": "U",
}

# GLYPHS as lookup tables over the 7-bit pattern read as a number (segment a
# is the high bit): which patterns are known, and which show something other
# than a blank or a dash. realign() scores thousands of shifts with these.
GLYPH_KNOWN = np.zeros(128, dtype=bool)
GLYPH_SHOWS = np.zeros(128, dtype=bool)
for _bits, _ch in GLYPHS.items():
    GLYPH_KNOWN[int(_bits, 2)] = True
    GLYPH_SHOWS[int(_bits, 2)] = _ch not in " -"

# Display segments are judged against the brightest segment in the same frame
# (see segment_states). A calibration may set "digit_ratio" (0 = fixed
# thresholds only); a display whose segments spread less than this is dark or
# fully lit, and falls back to the calibrated thresholds.
DIGIT_RATIO = 0.75
DIGIT_MIN_SPREAD = 40.0

# More lit indicators than this in one exclusive group cannot be a real panel
# (one is lit; bloom may tip a neighbour over) - it is a frame where the view is
# blocked or washed out, so it is reported as a decode problem.
MAX_LIT_PER_GROUP = 2

# Realignment (see Reader.realign): a frame that reads as nothing at all - no
# indicator lit, nothing on the display - may be a panel the camera has moved
# away from. At most once per REALIGN_EVERY seconds per machine, the decoder
# tries the whole calibration shifted across the camera frame, up to
# REALIGN_REACH of the calibrated frame's width and height either way, and
# adopts a shift only if it reads a believable panel there and nowhere else.
REALIGN_EVERY = 300
REALIGN_REACH = (0.25, 0.4)
REALIGN_STEP = 4
REALIGN_CLUSTER = 24.0
# A panel that is on always lights its selections - one cycle, one temperature
# and one dryness (or load and soil) - so a believable shift needs at least this
# many indicators lit across at least REALIGN_MIN_GROUPS exclusive groups. One
# lit "indicator" and a "1" is what the printed triangles and a bezel glint
# read as on a dark DR7 under IR, which 0.6.0 adopted.
REALIGN_MIN_LIT = 3
REALIGN_MIN_GROUPS = 2
# A shift this close to the current one is sampling noise, not a camera move:
# every region tolerates a few px, and chasing them only churns the reads.
REALIGN_MIN_MOVE = 8
REALIGN_UNREADABLE = 3

# Reading through one borderline segment (resolve_digit). On the real panels
# nearly every "?" was one segment box sitting a few px off its bar, so that it
# read within a few levels of its cut while the other six read squarely. Such
# a pattern is read as the digit that one flip makes, when exactly one
# segment within RESOLVE_BAND of the display's spread (brightest minus darkest
# segment) gives a digit, or one gives a digit RESOLVE_CLEAR times nearer its
# cut than any other - or the countdown names it (see expected()). 0.10 was
# chosen over 0.15 and 0.06 on 18 real DR7 and 5 TR7 frames read with six
# calibrations and every possible previous display: as many misreads fixed as
# 0.15 with half the wrong guesses (all from a stale countdown, which
# SNAP_WITHIN now rules out).
RESOLVE_BAND = 0.10
RESOLVE_CLEAR = 2.0
DIGIT_CHARS = "0123456789"
# A display that read as real glyphs is moved to the countdown's value
# (snap_to_expected) only while a countdown is really running: a one-minute
# tick seen within SNAP_WITHIN seconds. A display that is not counting - a
# knob being turned on a ready machine - is never pulled to its old value.
SNAP_WITHIN = 180

# Confirming a surprise (Reader._confirm). A display that reads "?" or changes
# to anything but itself or one minute less is read again, up to
# CONFIRM_FRAMES frames CONFIRM_GAP seconds apart, and a value is believed when
# two frames agree or one reads what the countdown expects. On a real DR7 a box
# half over a neighbour's glow read 68 as 58 for one frame; a second look reads
# 68. A failed snapshot is tried once more after FETCH_RETRY_DELAY seconds.
CONFIRM_FRAMES = 3
CONFIRM_GAP = 1.0
FETCH_RETRY_DELAY = 2.0

# Frames kept for checking a calibration (FrameStore). Misreads, reads that
# needed resolving or confirming, and one clean read of each display value per
# SAMPLE_EVERY seconds are saved beside the calibration with what the display
# really showed; a misread is labeled from the reads either side of it when the
# next clean read comes within LABEL_WITHIN seconds. keep_frames (default
# KEEP_FRAMES, 0 = off) caps them per machine, oldest dropped first.
KEEP_FRAMES = 200
SAMPLE_EVERY = 6 * 3600
LABEL_WITHIN = 300

# Fitting the digit boxes to the saved frames (fit_digits): a segment whose
# narrowest margin against its cut is under FIT_WEAK of the display's typical
# spread is tried up to FIT_REACH px either way, and moves only if that raises
# its margin by FIT_MIN_GAIN levels without losing a frame. A segment that
# already reads squarely is left where it is.
FIT_REACH = 4
FIT_MIN_GAIN = 3.0
FIT_WEAK = 0.2

# Slanted segment boxes (rect_median). A seven-segment display is usually
# italic - on the real DR7 and TR7 the bars lean about 10 degrees - and an
# upright box over a leaning bar covers the bar at one end and the dark gap
# (lit by its neighbour's glow) at the other; on the DR7 that read a 6's dark
# segment b as lit, 26 as 28. A digit's "slant" leans its boxes the same way,
# each about its own centre, so a box that sat right stays put. fit_digits
# tries each digit's slant over FIT_SLANTS degrees before it moves boxes.
MAX_SLANT = 30.0
FIT_SLANTS = tuple(range(-6, 21, 2))

# Payload keys the decoder owns. An indicator may not be named any of these,
# nor share a name with an exclusive group, or it would overwrite it.
RESERVED = {"display", "time_remaining", "state", "active", "raw",
            "decode_problem", "reader"}

# What the "reader" sensor reports: a clean read, a display that still reads
# as no glyph (or lost anchors), or a view that is blocked (more lights in an
# exclusive group than a panel can show - a TR7 lid stood open in front of its
# console, say). Hidden is not a misread, so it can be told apart in an alert.
READER_STATES = ("ok", "misread", "hidden")

# ---------------------------------------------------------------------------
# Machine profiles
#
# "exclusive" groups collapse to one sensor carrying the lit member's label —
# only one LED in them is ever lit. Every other group becomes one binary
# sensor per LED, because several can be lit at once.
#
# "states" is evaluated in order: the first rule with any named indicator lit
# wins. Nothing matching falls through to "ready" when the panel shows
# anything at all, otherwise "off". A non-numeric display always wins as
# "fault".
#
# Indicator names and groups must match what the calibration tool emits for
# the same machine; the tool is the source of truth for the click order and
# the human labels.
# ---------------------------------------------------------------------------
PROFILES = {
    "dr7": {
        "model": "DR7",
        "name": "Speed Queen DR7",
        "icon": "mdi:tumble-dryer",
        # Red: the DR7's lit segments clip toward white and sit on a lit blue
        # backlight field, so blue lights up the whole display block. The TR7
        # is the other way round — see its profile.
        "channel": "r",
        "exclusive": ("cycle", "temp", "dryness"),
        "group_icons": {
            "cycle": "mdi:format-list-bulleted",
            "temp": "mdi:thermometer",
            "dryness": "mdi:water-percent",
        },
        "states": (
            ("done", ("complete",)),
            ("cooling", ("cooling",)),
            ("running", ("sensing", "heating")),
        ),
        "active": ("running", "cooling", "ready"),
        "device_class": {
            "door_open": "door", "heating": "heat", "sensing": "running",
        },
        "labels": {
            "heavy_duty": "Heavy Duty", "regular": "Regular",
            "pet_items": "Pet Items", "time_dry": "Time Dry",
            "steam_sanitize": "Steam Sanitize", "perm_press": "Perm Press",
            "delicates": "Delicates", "pet_hair_remover": "Pet Hair Remover",
            "quick_dry": "Quick Dry", "steam_refresh": "Steam Refresh",
            "no_heat": "No Heat", "low": "Low", "medium": "Medium",
            "high": "High",
            "damp": "Damp", "less_dry": "Less Dry", "near_dry": "Near Dry",
            "dry": "Dry",
            "sensing": "Sensing", "heating": "Heating", "cooling": "Cooling",
            "complete": "Complete",
            "extended_tumble": "Extended Tumble", "steam_boost": "Steam Boost",
            "ecodry": "EcoDry", "anti_static": "Anti-Static",
            "signal": "Signal", "favorites": "Favorites",
            "door_open": "Door Open", "control_lock": "Control Lock",
            "extended": "Extended",
        },
    },
    # Read from a photograph of a real TR7 panel: ten wash cycles, three
    # collapsed selection rows (Temp, Load Size, Soil Level), six options,
    # two lock indicators and three status lights.
    #
    # Two names are deliberately not what the panel prints, because indicator
    # names share one flat payload namespace: the "Spin" wash cycle is
    # spin_cycle (the Status "Spin" light owns `spin`), and the Load Size and
    # Soil Level rows both print "Medium", so both rows are prefixed. The
    # labels are what reaches Home Assistant, and those match the panel.
    #
    # There is no Complete indicator on this panel, so `state` never reports
    # "done" for a TR7 — see the state rules below.
    "tr7": {
        "model": "TR7",
        "name": "Speed Queen TR7",
        "icon": "mdi:washing-machine",
        # Unlike the DR7, sample BLUE. The TR7's lit indicators are saturated
        # blue (R~60 G~57 B~243) while the unlit dots are neutral grey (~133
        # in every channel), so in red — and in luma — a lit LED reads DARKER
        # than an unlit one and every indicator decodes backwards. Blue splits
        # them by ~70 levels. The display behaves the same way: its segments
        # clear the backlight field by ~80 levels in blue and only ~12 in red.
        "channel": "b",
        "exclusive": ("cycle", "temp", "load_size", "soil"),
        "group_icons": {
            "cycle": "mdi:format-list-bulleted",
            "temp": "mdi:thermometer",
            "load_size": "mdi:weight",
            "soil": "mdi:liquid-spot",
        },
        # No "done": the panel has no Complete light, so the end of a cycle is
        # only visible as running -> ready. Detect it with an automation on
        # that transition until we know what the display does at the end.
        "states": (
            ("running", ("wash", "rinse", "spin")),
        ),
        "active": ("running", "ready"),
        # No device classes: Home Assistant reads the "lock" class as
        # on = Unlocked, which is backwards for an LED that lights when the
        # lock is engaged.
        "device_class": {},
        "labels": {
            # Group names double as the collapsed sensors' names.
            "temp": "Water Temp", "load_size": "Load Size",
            "soil": "Soil Level",
            "heavy_duty": "Heavy Duty", "perm_press": "Perm Press",
            "normal_eco": "Normal Eco", "delicate": "Delicate",
            "handwash": "Handwash", "favorites": "Favorites",
            "bulky": "Bulky", "rinse_and_spin": "Rinse & Spin",
            "spin_cycle": "Spin", "special_cycles": "Special Cycles",
            "temp_cold_cold": "Cold/Cold", "temp_warm_cold": "Warm/Cold",
            "temp_warm_warm": "Warm/Warm", "temp_hot_cold": "Hot/Cold",
            "load_small": "Small", "load_medium": "Medium",
            "load_large": "Large", "load_auto_fill": "Auto Fill",
            "soil_light": "Light", "soil_medium": "Medium",
            "soil_heavy": "Heavy", "soil_max": "Max",
            "wash": "Wash", "rinse": "Rinse", "spin": "Spin",
            "soak": "Soak", "pre_wash": "Pre-Wash",
            "extra_rinse": "Extra Rinse", "speed_cycle": "Speed Cycle",
            "delay_start": "Delay Start", "signal": "Signal",
            "control_lock": "Control Lock", "lid_lock": "Lid Lock",
        },
    },
}

log = logging.getLogger("sqpanel")


def env(name, default=None, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return str(raw).lower() in ("true", "1", "yes", "on")
    return cast(raw)


def homography(dst, src):
    """Coefficients (a..h) mapping each dst point to its src point:

        u = (a*x + b*y + c) / (g*x + h*y + 1),  v = (d*x + e*y + f) / (g*x + h*y + 1)

    — Pillow's PERSPECTIVE convention (output pixel -> input pixel). Four point
    pairs, no three collinear. sq-calibrate.html solves the same 8 equations,
    so the tool shows exactly the frame the decoder samples.
    """
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -x * u, -y * u]); rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -x * v, -y * v]); rhs.append(v)
    return tuple(float(c) for c in np.linalg.solve(np.array(rows, float), np.array(rhs, float)))


def parse_warp(raw):
    """Optional "warp": {"src": [[x, y] x4], "size": [w, h]}.

    src = the four corners clicked on the camera frame (top-left, top-right,
    bottom-right, bottom-left); size = the straightened frame they map onto.
    Every other coordinate in the file is in the straightened frame.
    """
    if not raw:
        return None
    try:
        src = [(float(p[0]), float(p[1])) for p in raw["src"]]
        w, h = int(raw["size"][0]), int(raw["size"][1])
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"warp needs src (4 points) and size: {exc}") from None
    if len(src) != 4 or w < 8 or h < 8:
        raise ValueError("warp needs exactly 4 src points and a size of at least 8 x 8")
    dst = [(0, 0), (w, 0), (w, h), (0, h)]
    try:
        coeffs = homography(dst, src)
    except np.linalg.LinAlgError:
        raise ValueError("warp corners are degenerate (three in a line?)") from None
    return {"src": src, "size": (w, h), "coeffs": coeffs}


def parse_digit_ratio(raw):
    """Optional "digit_ratio": 0 (fixed thresholds) up to just under 1."""
    if raw is None:
        return DIGIT_RATIO
    try:
        ratio = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"digit_ratio must be a number, not {raw!r}") from None
    if not 0 <= ratio < 1:
        raise ValueError(f"digit_ratio must be from 0 up to (not including) 1, not {ratio}")
    return ratio


def segment_states(values, thresholds, ratio=DIGIT_RATIO):
    """Which display segments are lit, from each one's median and threshold.

    A display's brightness — and the glow its lit segments throw onto unlit
    neighbours — changes with the time of day and the camera's exposure. On a
    real DR7 the lit segments read 158-190 one hour and 214-229 the next, while
    unlit ones beside them glowed up to 94 and then 135: any single fixed
    threshold ends up inside one of those ranges and a digit misreads. So while
    the display shows real contrast, a segment is lit when it reaches `ratio`
    of the brightest segment in the frame; the calibrated threshold only sets a
    floor (half of it) so a dim display is never read out of noise. A dark or
    uniformly lit display (spread under DIGIT_MIN_SPREAD) has no reference and
    uses the calibrated thresholds, as does ratio 0.
    """
    return [v >= c for v, c in zip(values, segment_cuts(values, thresholds, ratio))]


def segment_cuts(values, thresholds, ratio=DIGIT_RATIO):
    """The level each segment is judged against (see segment_states)."""
    if not values:
        return []
    hi, lo = max(values), min(values)
    if ratio <= 0 or hi - lo < DIGIT_MIN_SPREAD:
        return list(thresholds)
    cut = ratio * hi
    return [max(cut, t * 0.5) for t in thresholds]


def resolve_digit(bits, values, cuts, band, expect=(), allow_blank=False):
    """An unknown seven-segment pattern read as the digit one flip away.

    bits, values and cuts are one character's seven segments in SEG_ORDER.
    Only a segment within `band` of its cut may flip, and only to a digit (or
    a blank, for a leading character). If the countdown expects one of the
    candidates (`expect`, a set of characters), that one; otherwise the only
    candidate, or the one RESOLVE_CLEAR times nearer its cut than the next.
    Returns (character, segment index) or None - a pattern two segments from
    any digit, or two flips that fit equally well, stays "?".
    """
    if band <= 0:
        return None
    allowed = DIGIT_CHARS + (" " if allow_blank else "")
    found = []
    for j, (v, c) in enumerate(zip(values, cuts)):
        margin = abs(v - c)
        if margin > band:
            continue
        flipped = bits[:j] + ("0" if bits[j] == "1" else "1") + bits[j + 1:]
        ch = GLYPHS.get(flipped)
        if ch is not None and ch in allowed:
            found.append((margin, ch, j))
    if not found:
        return None
    hoped = [f for f in found if f[1] in expect]
    if len(hoped) == 1:
        return hoped[0][1], hoped[0][2]
    found.sort()
    if len(found) == 1 or found[1][0] > RESOLVE_CLEAR * found[0][0]:
        return found[0][1], found[0][2]
    return None


def snap_to_expected(patterns, values, cuts, band, expect):
    """A display that read as real glyphs but not what the countdown expects,
    read as the expected one when a single borderline segment is all that
    separates them. On a real DR7, a "6" whose segment e sat 8 levels under
    its cut read 68 as 58 frame after frame - a valid glyph, so nothing else
    catches it. A real change of display has its segments read squarely, so
    no flip within `band` reaches the old value and it stands.

    patterns: each character's seven bits as read. Returns
    (text, character index, segment index) or None.
    """
    if band <= 0:
        return None
    n = len(SEG_ORDER)
    found = []
    for want in expect:
        if len(want) != len(patterns):
            continue
        flips = []
        for i, (bits, ch) in enumerate(zip(patterns, want)):
            target = GLYPH_BITS.get(ch)
            if target is None:
                break
            flips += [(i, j) for j in range(n) if bits[j] != target[j]]
        else:
            if len(flips) == 1:
                i, j = flips[0]
                margin = abs(values[i * n + j] - cuts[i * n + j])
                if margin <= band:
                    found.append((margin, want, i, j))
    if not found:
        return None
    found.sort()
    if len(found) == 1 or found[1][0] > RESOLVE_CLEAR * found[0][0]:
        return found[0][1], found[0][2], found[0][3]
    return None


def expected(last):
    """What the display may show next after `last`: itself, or a running
    countdown one minute on (either way a one-digit number is padded)."""
    if not last:
        return set()
    out = {last}
    shown = last.strip()
    if shown.isdigit() and int(shown) > 0:
        n, w = str(int(shown) - 1), len(last)
        out |= {n.rjust(w), n.zfill(w)}
    return out


def segment_margins(values, cuts, text):
    """How far each segment reads on the right side of its cut for `text`
    (negative = the wrong side), or None for a character that is no glyph."""
    if len(text) * len(SEG_ORDER) != len(values):
        return None
    want = ""
    for ch in text:
        bits = GLYPH_BITS.get(ch)
        if bits is None:
            return None
        want += bits
    return [(v - c) if b == "1" else (c - v) for v, c, b in zip(values, cuts, want)]


# The other way round from GLYPHS: a character's seven segments.
GLYPH_BITS = {ch: bits for bits, ch in GLYPHS.items()}


def crowded_groups(profile, leds):
    """Exclusive groups with more lit indicators than a real panel shows."""
    return crowded(profile["exclusive"], leds)


def straighten(img, warp):
    """The camera frame perspective-corrected onto the calibration's frame."""
    if not warp:
        return img
    return img.transform(warp["size"], Image.PERSPECTIVE, warp["coeffs"], Image.BILINEAR)


def shifted_warp(warp, dx, dy, size):
    """The calibration's straightening with the camera frame moved by (dx, dy).

    A panel that sits dx right / dy down of where it was calibrated is read by
    moving the four source corners the same way. A calibration without a warp
    is the identity over its own frame, so it gets one that only translates.
    """
    if not dx and not dy:
        return warp
    if warp:
        src = [(x + dx, y + dy) for x, y in warp["src"]]
        w, h = warp["size"]
    else:
        w, h = size
        src = [(dx, dy), (w + dx, dy), (w + dx, h + dy), (dx, h + dy)]
    return parse_warp({"src": src, "size": [w, h]})


def to_frame(warp, x, y):
    """A calibration point -> the camera-frame pixel it is straightened from."""
    if not warp:
        return x, y
    a, b, c, d, e, f, g, h = warp["coeffs"]
    k = g * x + h * y + 1.0
    return (a * x + b * y + c) / k, (d * x + e * y + f) / k


def parse_slant(raw, where="digit_slant"):
    """A digit's lean in degrees: 0 upright, positive when the top leans
    right (an italic display). Each segment box is then sampled as a
    parallelogram leaning the same way (see rect_median)."""
    if raw is None:
        return 0.0
    try:
        deg = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{where} must be a number of degrees, not {raw!r}") from None
    if not -MAX_SLANT <= deg <= MAX_SLANT:
        raise ValueError(f"{where} must be within +/-{MAX_SLANT} degrees, not {deg}")
    return deg


class Calibration:
    def __init__(self, path=None, data=None, quiet=False):
        if data is None:
            with open(path) as fh:
                data = json.load(fh)
        if data.get("version") != 1:
            raise ValueError("Unsupported calibration version")
        self.machine = data.get("machine")
        self.channel = data.get("channel", "r")
        self.radius = int(data.get("led_radius", 6))
        self.size = tuple(data.get("image_size", (0, 0)))
        self.anchors = data.get("anchors", [])
        self.leds = data["leds"]
        self.digits = data["digits"]
        self.warp = parse_warp(data.get("warp"))
        self.digit_ratio = parse_digit_ratio(data.get("digit_ratio"))
        # The lean of the display's digits: "digit_slant" for all of them,
        # or "slant" on one digit. tan of it is what sampling uses.
        default = parse_slant(data.get("digit_slant"))
        self.slants = {d["index"]: math.tan(math.radians(
            parse_slant(d.get("slant", default), f"digit {d['index']} slant")))
            for d in self.digits}
        if len(self.anchors) != 2 and not quiet:
            log.warning("Expected 2 anchors, got %d — drift correction disabled",
                        len(self.anchors))


def channel_plane(img, which):
    arr = np.asarray(img, dtype=np.float32)
    if which == "r":
        return arr[:, :, 0]
    if which == "g":
        return arr[:, :, 1]
    if which == "b":
        return arr[:, :, 2]
    return 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]


def luma_plane(img):
    return channel_plane(img, "l")


ANCHOR_WINDOW = 26


def anchor_window(anchors):
    """Half-width of the box each anchor is searched in.

    The usual 26 px, but never so wide that one anchor's box reaches the other:
    in a tightly cropped frame the ▲ and ▼ can sit under 30 px apart, and a box
    holding both triangles puts both centroids between them — the transform
    then reads the panel as half size and every region lands in the wrong place.
    """
    if len(anchors) != 2:
        return ANCHOR_WINDOW
    gap = ((anchors[1]["x"] - anchors[0]["x"]) ** 2
           + (anchors[1]["y"] - anchors[0]["y"]) ** 2) ** 0.5
    return max(6, min(ANCHOR_WINDOW, int(gap * 0.45)))


def locate_anchor(plane, x, y, win=ANCHOR_WINDOW):
    """Find the bright printed triangle near (x, y) and return its centroid."""
    h, w = plane.shape
    x0, x1 = max(0, int(x - win)), min(w, int(x + win))
    y0, y1 = max(0, int(y - win)), min(h, int(y + win))
    patch = plane[y0:y1, x0:x1]
    if patch.size == 0:
        return None, 0.0
    lo, hi = float(patch.min()), float(patch.max())
    contrast = hi - lo
    if contrast < 40:
        return None, contrast
    mask = patch >= (lo + hi) / 2.0
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None, contrast
    return (x0 + xs.mean(), y0 + ys.mean()), contrast


class Transform:
    """Similarity transform derived from two anchor correspondences."""

    def __init__(self, src=None, dst=None):
        self.ok = False
        self.ox = self.oy = 0.0
        self.scale = 1.0
        self.cos = 1.0
        self.sin = 0.0
        self.sx = self.sy = 0.0
        if not src or not dst:
            return
        (ax, ay), (bx, by) = src
        (cx, cy), (dx, dy) = dst
        sv = complex(bx - ax, by - ay)
        dv = complex(dx - cx, dy - cy)
        if abs(sv) < 1e-6 or abs(dv) < 1e-6:
            return
        ratio = dv / sv
        self.scale = abs(ratio)
        self.cos = ratio.real / self.scale
        self.sin = ratio.imag / self.scale
        self.sx, self.sy = ax, ay
        self.ox, self.oy = cx, cy
        self.ok = True

    def point(self, x, y):
        if not self.ok:
            return x, y
        dx, dy = (x - self.sx) * self.scale, (y - self.sy) * self.scale
        return (self.ox + dx * self.cos - dy * self.sin,
                self.oy + dx * self.sin + dy * self.cos)

    def length(self, v):
        return v * self.scale if self.ok else v


def disc_median(plane, cx, cy, r):
    h, w = plane.shape
    x0, x1 = max(0, int(cx - r)), min(w, int(cx + r) + 1)
    y0, y1 = max(0, int(cy - r)), min(h, int(cy + r) + 1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    patch = plane[y0:y1, x0:x1]
    yy, xx = np.ogrid[y0:y1, x0:x1]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    vals = patch[mask]
    return float(np.median(vals)) if vals.size else 0.0


def rect_median(plane, rect, inset=0.2, slant=0.0):
    """Median over a segment box, inset by `inset` of its size each side.

    With `slant` (tan of the lean) the box is a parallelogram leaning about
    its own centre: each pixel row shifts right by `slant` px for every px it
    sits above the centre, the way an italic digit's bars lean.
    """
    x, y, w, h = rect[:4]
    ix, iy = w * inset, h * inset
    h_img, w_img = plane.shape
    y0, y1 = max(0, int(y + iy)), min(h_img, int(y + h - iy) + 1)
    if not slant:
        x0, x1 = max(0, int(x + ix)), min(w_img, int(x + w - ix) + 1)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        patch = plane[y0:y1, x0:x1]
        return float(np.median(patch)) if patch.size else 0.0
    cy = y + h / 2.0
    rows = []
    for row in range(y0, y1):
        shift = (cy - (row + 0.5)) * slant
        a, b = max(0, int(x + ix + shift)), min(w_img, int(x + w - ix + shift) + 1)
        if b > a:
            rows.append(plane[row, a:b])
    return float(np.median(np.concatenate(rows))) if rows else 0.0


def seg_corners(rect, slant=0.0):
    """The four corners of a segment box as sampled (top-left, top-right,
    bottom-right, bottom-left), leaning with `slant`."""
    x, y, w, h = rect[:4]
    lean = h / 2.0 * slant
    return [(x + lean, y), (x + w + lean, y), (x + w - lean, y + h), (x - lean, y + h)]


# Home Assistant's API as an add-on sees it (homeassistant_api: true). A
# machine with camera_entity reads that camera through it; only requests to it
# carry the add-on's token, never a snapshot_url pointing anywhere else.
HA_API = os.environ.get("HA_API_URL", "http://supervisor/core/api").rstrip("/")


def camera_url(entity):
    return f"{HA_API}/camera_proxy/{entity}"


def fetch(url, timeout=10):
    headers = {"User-Agent": "sq-panel/0.2"}
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token and url.startswith(HA_API + "/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_retry(url, name=""):
    """fetch(), tried once more after FETCH_RETRY_DELAY seconds. A room
    camera behind Home Assistant now and then answers 500 or times out once;
    a second try usually reads, and a camera that is really gone fails both."""
    try:
        return fetch(url)
    except (urllib.error.URLError, OSError) as exc:
        log.info("[%s] Snapshot did not load (%s) - trying once more", name, exc)
    time.sleep(FETCH_RETRY_DELAY)
    return fetch(url)


def label_for(profile, leds, name):
    """Calibration labels win, then the profile's, then the bare name."""
    entry = leds.get(name) if isinstance(leds, dict) else None
    if entry and entry.get("label"):
        return entry["label"]
    return profile["labels"].get(name, name.replace("_", " ").capitalize())


class Capture:
    """One camera frame and what reading it gave: the JPEG as fetched, the
    decoded result tuple, the display as resolved (`text`) and as the segments
    literally read (`literal`), each segment's level and cut, the band a
    borderline segment had to fall in, any resolved segments, and where the
    calibration sat in the camera frame (`src`, `offset`) - everything a
    FrameStore needs to save it and later label and re-check it."""

    def __init__(self, jpeg, image, result, reader):
        self.jpeg = jpeg
        self.image = image
        self.result = result
        self.text = result[2]
        self.leds = result[1]
        self.literal = reader.literal
        self.values = [round(v, 1) for v in reader.values]
        self.cuts = [round(c, 1) for c in reader.cuts]
        self.band = round(reader.band, 1)
        self.resolved = list(reader.resolved)
        self.src = [list(p) for p in reader.warp["src"]] if reader.warp else None
        self.offset = reader.offset
        self.hidden = bool(crowded(reader.exclusive, result[1]))
        self.settled = False         # a closer look agreed with it (see _confirm)


def crowded(exclusive, leds):
    """Exclusive groups with more lit indicators than a real panel shows."""
    return [g for g in exclusive
            if sum(1 for i in leds.values() if i["group"] == g and i["on"])
            > MAX_LIT_PER_GROUP]


class Reader:
    def __init__(self, cal, url, exclusive=(), name=""):
        self.cal = cal
        self.url = url
        self.exclusive = tuple(exclusive)
        self.name = name
        # Where the panel is now relative to the calibration, in camera-frame
        # pixels: found by realign() and kept for this calibration (a reload
        # starts again from (0, 0) and finds it on the first dark frame).
        self.offset = (0, 0)
        self.warp = cal.warp
        self.next_realign = 0.0
        self.unreadable = 0                 # reads in a row with an unknown glyph
        # The last display read with no "?" (and the view not blocked): what
        # expected() works from, to resolve a digit and to spot a surprise.
        self.last_clean = None
        self.expect = set()
        self.ticked_at = None               # monotonic time of the last minute tick seen
        self.counting = False               # a tick within SNAP_WITHIN: snapping allowed
        # Every frame the last read() looked at, and the one it returned.
        self.captures = []
        self.chosen = None
        # Set by each _sample(): the display as literally read, each segment's
        # level and cut, the band a borderline segment must fall in, and any
        # segment resolve_digit read the other way.
        self.literal = ""
        self.values, self.cuts = [], []
        self.band = 0.0
        self.resolved = []

    def read(self):
        """Read the panel: one frame, more if the display surprises (see
        _confirm), and a search for the panel if it reads as lost."""
        self.captures = []
        self.expect = expected(self.last_clean)
        self.counting = (self.ticked_at is not None
                         and time.monotonic() - self.ticked_at <= SNAP_WITHIN)
        cap = self._capture()
        if not cap.hidden and self._surprising(cap.text):
            cap = self._confirm(cap)
        _, leds, text, anchor_ok, _ = cap.result
        self.unreadable = self.unreadable + 1 if "?" in text else 0
        if lost(leds, text, anchor_ok, self.unreadable) and time.monotonic() >= self.next_realign:
            self.next_realign = time.monotonic() + REALIGN_EVERY
            if self.realign(cap.image):
                again = self._decode(cap.jpeg, cap.image)
                self.captures[self.captures.index(cap)] = again
                cap = again
        self.chosen = cap
        if "?" not in cap.text and cap.result[3] and not cap.hidden:
            if cap.text != self.last_clean and cap.text in self.expect:
                self.ticked_at = time.monotonic()        # a minute ticked down
            self.last_clean = cap.text
        return cap.result

    def _surprising(self, text):
        """A "?", or a display that is neither the last one nor a minute less."""
        return "?" in text or (self.last_clean is not None and text not in self.expect)

    def _capture(self):
        jpeg = fetch_retry(self.url, self.name)
        cap = self._decode(jpeg, Image.open(io.BytesIO(jpeg)).convert("RGB"))
        self.captures.append(cap)
        return cap

    def _decode(self, jpeg, image):
        cap = Capture(jpeg, image, self._sample(straighten(image, self.warp), resolve=True), self)
        if cap.resolved:
            log.info("[%s] Display %r read as %r - %s", self.name, cap.literal, cap.text,
                     ", ".join(f"digit {r['digit'] + 1} segment {r['segment']} at "
                               f"{r['value']:.0f} against its cut {r['cut']:.0f}"
                               for r in cap.resolved))
        return cap

    def _confirm(self, first):
        """Read a surprising display again before believing it.

        Up to CONFIRM_FRAMES frames, CONFIRM_GAP seconds apart. A frame with no
        "?" that reads what the countdown expects, or what an earlier frame
        read, settles it. Otherwise the last frame that read cleanly wins, or
        failing that the last frame - a display that changes every frame (a
        flashing code, a knob being turned) is reported as it stands.
        """
        reads, chosen = [first], None
        for _ in range(CONFIRM_FRAMES - 1):
            time.sleep(CONFIRM_GAP)
            try:
                cap = self._capture()
            except (urllib.error.URLError, OSError):
                break
            if "?" not in cap.text and (cap.text in self.expect
                                        or any(r.text == cap.text for r in reads)):
                chosen = cap
                chosen.settled = True
                break
            reads.append(cap)
        if chosen is None:
            clean = [r for r in reads if "?" not in r.text]
            chosen = (clean or reads)[-1]
        if chosen.text != first.text:
            log.info("[%s] Display read %r, on a closer look %r", self.name,
                     first.text, chosen.text)
        return chosen

    def _sample(self, img, anchors=True, resolve=False):
        plane = channel_plane(img, self.cal.channel)
        lum = luma_plane(img)

        tf, anchor_ok = self._align(lum) if anchors else (Transform(), True)

        leds = {}
        overlay = []
        for led in self.cal.leds:
            x, y = tf.point(led["x"], led["y"])
            r = max(2.0, tf.length(self.cal.radius))
            value = disc_median(plane, x, y, r)
            on = value >= led["threshold"]
            leds[led["name"]] = {"on": on, "value": round(value, 1),
                                 "group": led["group"],
                                 "label": led.get("label")}
            overlay.append(("disc", x, y, r, on))

        digits = sorted(self.cal.digits, key=lambda d: d["index"])
        rects, values, thresholds = [], [], []
        for digit in digits:
            lean = self.cal.slants.get(digit["index"], 0.0)
            for seg in SEG_ORDER:
                spec = digit["segments"][seg]
                rx, ry, rw, rh = spec["rect"]
                px, py = tf.point(rx, ry)
                rect = (px, py, tf.length(rw), tf.length(rh), lean)
                rects.append(rect)
                values.append(rect_median(plane, rect, slant=lean))
                thresholds.append(spec["threshold"])
        cuts = segment_cuts(values, thresholds, self.cal.digit_ratio)
        states = [v >= c for v, c in zip(values, cuts)]
        # How squarely each display segment reads - far above or far below the
        # level it is judged against, not straddling an edge. realign() uses it
        # to place the panel to the pixel once the indicators agree.
        self.sharpness = sum(abs(v - c) for v, c in zip(values, cuts))
        # A dark or uniformly lit display has no spread to judge "borderline"
        # by, so nothing on it is resolved.
        spread = (max(values) - min(values)) if values else 0.0
        self.band = RESOLVE_BAND * spread if spread >= DIGIT_MIN_SPREAD else 0.0
        self.values, self.cuts, self.resolved = values, cuts, []
        n = len(SEG_ORDER)
        text = literal = ""
        patterns = []
        for i in range(len(digits)):
            part = slice(i * n, (i + 1) * n)
            bits = ""
            for j in range(n):
                on = states[i * n + j]
                bits += "1" if on else "0"
                overlay.append(("rect", rects[i * n + j], on))
            patterns.append(bits)
            ch = GLYPHS.get(bits, "?")
            literal += ch
            if ch == "?" and resolve:
                hope = {e[i] for e in self.expect if len(e) == len(digits)}
                got = resolve_digit(bits, values[part], cuts[part], self.band, hope,
                                    allow_blank=(i == 0 and len(digits) > 1))
                if got:
                    ch, j = got
                    k = i * n + j
                    self.resolved.append({"digit": i, "segment": SEG_ORDER[j],
                                          "value": round(values[k], 1),
                                          "cut": round(cuts[k], 1), "as": ch})
                    overlay.append(("mark", rects[k]))
            text += ch
        if resolve and self.counting and not self.resolved and "?" not in text \
                and self.expect and text not in self.expect:
            got = snap_to_expected(patterns, values, cuts, self.band, self.expect)
            if got:
                text, i, j = got
                k = i * n + j
                self.resolved.append({"digit": i, "segment": SEG_ORDER[j],
                                      "value": round(values[k], 1),
                                      "cut": round(cuts[k], 1), "as": text[i]})
                overlay.append(("mark", rects[k]))
        self.literal = literal
        if self.resolved:
            overlay.append(("note", f"{literal!r} read as {text!r}"))

        if self.offset != (0, 0):
            overlay.append(("note", f"shift {self.offset[0]:+d},{self.offset[1]:+d}"))
        return img, leds, text, anchor_ok, overlay

    def realign(self, raw):
        """Find the panel again after the camera moved. True if it shifted.

        A coarse pass maps every region back into the camera frame once and
        scores a grid of shifts together from box means (_coarse); a 1 px pass
        then reads the best of them through the real straighten-and-sample
        path. A shift counts only if it reads a believable panel: a character
        on the display, no unknown glyph, at least REALIGN_MIN_LIT indicators
        lit across REALIGN_MIN_GROUPS exclusive groups, and no group with two. The panel is where the most indicators light
        together, and those shifts must sit in one patch; within it the display
        settles the pixel, where its segments read most squarely on or off. A
        blank panel reads nothing anywhere, and a frame that matches in two
        places is left alone, so neither moves the calibration. Anchors are
        left out of the search (they would pull every nearby shift onto the
        same read); the normal read after it applies them as usual.
        """
        cal, base = self.cal, self.cal.warp
        plane = channel_plane(raw, cal.channel)
        h_img, w_img = plane.shape
        if base:
            fw, fh = base["size"]
            (x0, y0), (x1, y1), (x2, y2), (x3, y3) = base["src"]
            area = abs((x0 * y1 - x1 * y0) + (x1 * y2 - x2 * y1)
                       + (x2 * y3 - x3 * y2) + (x3 * y0 - x0 * y3)) / 2.0
            scale = (area / float(fw * fh)) ** 0.5
        else:
            fw, fh = cal.size if cal.size[0] else (w_img, h_img)
            scale = 1.0
        r = max(2, int(round(cal.radius * scale)))

        leds = [(led, *to_frame(base, led["x"], led["y"])) for led in cal.leds]
        segs = []
        for digit in sorted(cal.digits, key=lambda d: d["index"]):
            lean = cal.slants.get(digit["index"], 0.0)
            for seg in SEG_ORDER:
                spec = digit["segments"][seg]
                x, y, w, h = spec["rect"]
                ix, iy = w * 0.2, h * 0.2
                pts = [to_frame(base, px, py) for px, py in
                       seg_corners((x + ix, y + iy, w - 2 * ix, h - 2 * iy), lean)]
                segs.append((min(q[0] for q in pts), min(q[1] for q in pts),
                             max(q[0] for q in pts), max(q[1] for q in pts),
                             spec["threshold"]))

        def believable(dx, dy):
            """(indicators lit, digit sharpness) of the real read at this shift;
            (-1, 0) if it is not a believable panel.

            Runs the same straighten-and-sample path as every poll, so what is
            adopted is exactly what will be read."""
            _, leds_, text, _, overlay = self._sample(
                straighten(raw, shifted_warp(base, dx, dy, (fw, fh))), anchors=False)
            lit, groups = 0, {}
            for i in leds_.values():
                if i["on"]:
                    lit += 1
                    if i["group"] in self.exclusive:
                        groups[i["group"]] = groups.get(i["group"], 0) + 1
            if (lit < REALIGN_MIN_LIT or len(groups) < REALIGN_MIN_GROUPS
                    or "?" in text or not text.strip(" -")
                    or any(n > 1 for n in groups.values())):
                return (-1, 0.0)
            return (lit, self.sharpness)

        reach_x = int(fw * REALIGN_REACH[0] * scale)
        reach_y = int(fh * REALIGN_REACH[1] * scale)
        coarse = self._coarse(plane, leds, segs, r, reach_x, reach_y)
        most = max(sc[0] for _, _, sc in coarse)
        if most < 0:
            log.info("[%s] Nothing readable within %d x %d px of the calibration - "
                     "panel off, or the camera moved further than that",
                     self.name, reach_x, reach_y)
            return False
        # The panel is where the most indicators light at once; those shifts
        # form one patch around the true position (an indicator reads lit while
        # its disc stays on the LED's glow). Anywhere else lighting as many
        # means the frame matches in more than one place - not safe to pick.
        top = [(dx, dy, sc) for dx, dy, sc in coarse if sc[0] == most]
        bx, by, _ = max(top, key=lambda t: t[2][1])
        if any(((dx - bx) ** 2 + (dy - by) ** 2) ** 0.5 > REALIGN_CLUSTER for dx, dy, _ in top):
            log.warning("[%s] Panel lost and more than one position reads it - not "
                        "realigning; check the camera", self.name)
            return False
        # Climb at 1 px with the real read path from the sharpest of them (the
        # coarse pass works on box means, so it can land a few px off).
        seen = {}
        fx, fy = bx, by
        for _ in range(REALIGN_STEP * 4):
            for dy in range(fy - 2, fy + 3):
                for dx in range(fx - 2, fx + 3):
                    if (dx, dy) not in seen:
                        seen[(dx, dy)] = believable(dx, dy)
            nx, ny = max(((q, sc) for q, sc in seen.items()
                          if abs(q[0] - fx) <= 2 and abs(q[1] - fy) <= 2),
                         key=lambda t: t[1])[0]
            if (nx, ny) == (fx, fy):
                break
            fx, fy = nx, ny
        if seen[(fx, fy)][0] < 0:
            log.info("[%s] Coarse match did not read as a panel up close - not realigning", self.name)
            return False
        if ((fx - self.offset[0]) ** 2 + (fy - self.offset[1]) ** 2) ** 0.5 < REALIGN_MIN_MOVE:
            return False
        log.warning("[%s] Panel moved in the camera frame - realigned by %+d, %+d px "
                    "(was %+d, %+d). Recalibrate if it keeps moving.",
                    self.name, fx, fy, self.offset[0], self.offset[1])
        self.offset = (fx, fy)
        self.warp = shifted_warp(base, fx, fy, (fw, fh))
        return True

    def _coarse(self, plane, leds, segs, r, reach_x, reach_y):
        """The coarse grid of realign(), scored all at once.

        Box means from a summed-area table stand in for the medians here (one
        array per region across every candidate shift); the same believability
        rules apply, and the 1 px refinement after it reads for real. Each
        entry is (dx, dy, (indicators lit or -1, digit sharpness)).
        """
        h_img, w_img = plane.shape
        sat = np.zeros((h_img + 1, w_img + 1), dtype=np.float64)
        sat[1:, 1:] = plane.astype(np.float64).cumsum(0).cumsum(1)
        xs = np.arange(-reach_x, reach_x + 1, REALIGN_STEP)
        ys = np.arange(-reach_y, reach_y + 1, REALIGN_STEP)
        DX, DY = np.meshgrid(xs, ys)

        def box(x0, y0, x1, y1):
            x0 = np.clip(x0, 0, w_img); x1 = np.clip(x1, 0, w_img)
            y0 = np.clip(y0, 0, h_img); y1 = np.clip(y1, 0, h_img)
            area = (x1 - x0) * (y1 - y0)
            total = sat[y1, x1] - sat[y0, x1] - sat[y1, x0] + sat[y0, x0]
            return np.where(area > 0, total / np.maximum(area, 1), 0.0)

        half = max(1, int(r * 0.7))           # the square inside the sampling disc
        lit = np.zeros(DX.shape, dtype=np.int32)
        sharp = np.zeros(DX.shape, dtype=np.float64)
        groups = {}
        for led, cx, cy in leds:
            x, y = int(round(cx)), int(round(cy))
            v = box(x + DX - half, y + DY - half, x + DX + half + 1, y + DY + half + 1)
            on = v >= led["threshold"]
            lit += on
            if led["group"] in self.exclusive:
                groups[led["group"]] = groups.get(led["group"], 0) + on
        crowded = np.zeros(DX.shape, dtype=bool)
        chosen = np.zeros(DX.shape, dtype=np.int32)        # exclusive groups with a light
        for n in groups.values():
            crowded |= n > 1
            chosen += n > 0

        n_seg = len(SEG_ORDER)
        if segs:
            vals = np.stack([box(int(sx0) + DX, int(sy0) + DY, int(sx1) + 1 + DX, int(sy1) + 1 + DY)
                             for sx0, sy0, sx1, sy1, _ in segs])
            thr = np.array([t for *_, t in segs], dtype=np.float64)[:, None, None]
            hi, lo = vals.max(0), vals.min(0)
            ratio = self.cal.digit_ratio
            relative = (ratio > 0) & (hi - lo >= DIGIT_MIN_SPREAD)
            states = np.where(relative, vals >= np.maximum(ratio * hi, thr * 0.5), vals >= thr)
            cuts = np.where(relative, np.maximum(ratio * hi, thr * 0.5), thr)
            sharp = np.abs(vals - cuts).sum(0)
            readable = np.ones(DX.shape, dtype=bool)
            showing = np.zeros(DX.shape, dtype=bool)
            for i in range(len(segs) // n_seg):
                code = np.zeros(DX.shape, dtype=np.int32)
                for j in range(n_seg):
                    code = code * 2 + states[i * n_seg + j]
                readable &= GLYPH_KNOWN[code]
                showing |= GLYPH_SHOWS[code]
        else:
            readable = np.ones(DX.shape, dtype=bool)
            showing = np.zeros(DX.shape, dtype=bool)
        ok = (lit >= REALIGN_MIN_LIT) & (chosen >= REALIGN_MIN_GROUPS) & ~crowded & readable & showing
        score = np.where(ok, lit, -1)
        return [(int(dx), int(dy), (int(n), float(sh) if n >= 0 else 0.0))
                for dx, dy, n, sh in zip(DX.ravel(), DY.ravel(), score.ravel(), sharp.ravel())]

    def _align(self, lum):
        if len(self.cal.anchors) != 2:
            return Transform(), True
        found = []
        win = anchor_window(self.cal.anchors)
        for a in self.cal.anchors:
            pt, contrast = locate_anchor(lum, a["x"], a["y"], win)
            if pt is None:
                log.warning("Anchor %s not found (contrast %.0f)", a["name"], contrast)
                return Transform(), False
            found.append(pt)
        src = [(self.cal.anchors[0]["x"], self.cal.anchors[0]["y"]),
               (self.cal.anchors[1]["x"], self.cal.anchors[1]["y"])]
        tf = Transform(src, found)
        if tf.ok and (tf.scale < 0.85 or tf.scale > 1.18):
            log.warning("Anchor scale %.2f outside tolerance", tf.scale)
            return tf, False
        return tf, True


def lost(leds, text, anchor_ok, unreadable=0):
    """A read that may mean the camera moved: nothing lit and nothing on the
    display, anchors that were not found, or a display that has read as an
    unknown glyph REALIGN_UNREADABLE times running.

    A single "?" is a digit misread (glow, a frame caught mid-change), not a
    moved camera: on a real TR7 one started a search that adopted a 4 px shift
    and made the next digits misread more often (0.6.1). A camera that moved
    stays unreadable."""
    dark = not any(i["on"] for i in leds.values()) and not text.strip(" -")
    return dark or not anchor_ok or unreadable >= REALIGN_UNREADABLE


def annotate(img, overlay, text):
    out = img.copy()
    draw = ImageDraw.Draw(out)
    lit, unlit = (91, 225, 255), (240, 160, 43)
    notes = []
    for item in overlay:
        if item[0] == "disc":
            _, x, y, r, on = item
            draw.ellipse([x - r, y - r, x + r, y + r],
                         outline=lit if on else unlit, width=2)
        elif item[0] == "note":
            notes.append(item[1])
        elif item[0] == "mark":
            # a segment resolve_digit read the other way
            x, y, w, h = item[1][:4]
            lean = item[1][4] if len(item[1]) > 4 else 0.0
            draw.polygon(seg_corners((x - 2, y - 2, w + 4, h + 4), lean), outline=(255, 64, 200), width=2)
        else:
            _, rect, on = item
            lean = rect[4] if len(rect) > 4 else 0.0
            draw.polygon(seg_corners(rect, lean), outline=lit if on else unlit, width=2)
    draw.text((12, 12), " ".join([f"read: {text!r}"] + notes), fill=(255, 255, 255))
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def interpret(profile, leds, text):
    """Collapse raw ROI results into the published entity values."""
    out = {}
    exclusive = profile["exclusive"]

    for group in exclusive:
        winner, best = None, -1.0
        for name, info in leds.items():
            if info["group"] == group and info["on"] and info["value"] > best:
                winner, best = name, info["value"]
        out[group] = label_for(profile, leds, winner) if winner else "None"

    for name, info in leds.items():
        if info["group"] not in exclusive:
            out[name] = "ON" if info["on"] else "OFF"

    display = text.strip()
    out["display"] = display if display else "--"
    out["time_remaining"] = int(display) if display.isdigit() else None

    fault = bool(display) and not display.isdigit() and display != "-"
    lit = {n for n, i in leds.items() if i["on"]}

    state = None
    if fault:
        state = "fault"
    else:
        for candidate, names in profile["states"]:
            if lit.intersection(names):
                state = candidate
                break
    if state is None:
        any_exclusive = any(out[g] != "None" for g in exclusive)
        state = "ready" if (display or any_exclusive) else "off"

    out["state"] = state
    out["active"] = state in profile["active"]
    return out


# ---------------------------------------------------------------------------
# Saved frames
#
# A calibration is only as good as the frames it was checked on, and the frames
# that matter are the ones that went wrong. So the add-on keeps them: every
# misread, every read that needed a borderline segment resolved or a closer
# look, and one clean read of each display value now and then, each with what
# the display really showed. A calibration that loads is checked against all
# of them (check_frames), and fit_digits moves the digit boxes to where they
# read them best. None of this is in the decode path: a read never depends on
# a saved frame.
# ---------------------------------------------------------------------------


def wrong_side(meta, text):
    """How far, in total, the saved frame's segments sit on the wrong side of
    their cuts for `text`, beyond the borderline band; None if it is no glyph."""
    margins = segment_margins(meta.get("values") or [], meta.get("cuts") or [], text)
    if margins is None:
        return None
    band = meta.get("band") or 0.0
    return sum(max(0.0, -m - band) for m in margins)


class FrameStore:
    """Frames worth keeping for one machine, saved beside its calibration.

    Each is <folder>/<YYYYmmdd-HHMMSS>-<kind>.jpg - the camera's JPEG as
    fetched - with a .json beside it: when, what was read (resolved and
    literal), the label (what the display really showed, or null) and where it
    came from, every segment's level and cut, and where the calibration sat in
    the camera frame. Kinds:

    - misread: still "?" after a closer look. Labeled from the clean reads
      either side when the next one comes (see _label).
    - resolved: a digit read through a borderline segment. Labeled the same
      way, so a resolution that guessed wrong shows up in a check.
    - disagreed: a frame of a closer look that read something other than the
      frames that settled it, labeled with what they settled on.
    - sample: a clean read, at most once per display value per SAMPLE_EVERY,
      labeled with itself - what keeps every lit and unlit segment covered.
    """

    def __init__(self, folder, keep=KEEP_FRAMES, name=""):
        self.folder, self.keep, self.name = folder, int(keep), name
        self.pending = []            # (stem, time saved, clean read before it)
        self.sampled = {}            # display -> when it was last kept as a sample
        self.last_clean = None

    def record(self, captures, chosen, expect=(), counting=False):
        """Save what is worth keeping from one poll's frames. Never fatal.

        `expect` is what the countdown allowed before this read (see
        expected()); `counting`, whether a countdown was ticking."""
        if self.keep <= 0 or chosen is None:
            return
        try:
            self._record(captures, chosen, set(expect), counting)
        except OSError as exc:
            log.warning("[%s] Could not save a frame to %s: %s", self.name, self.folder, exc)

    def _record(self, captures, chosen, expect=frozenset(), counting=False):
        now = time.time()
        final = chosen.text
        if chosen.hidden or not chosen.result[3]:
            return                   # a blocked view or lost anchors: nothing to learn
        if "?" in final:
            self.pending.append((self.save(chosen, "misread"), now, self.last_clean))
            return
        if chosen.settled:           # only a settled read can label the frames it overruled
            for cap in captures:
                if cap is not chosen and cap.text != final and not cap.hidden:
                    self.save(cap, "disagreed", label=final, source="closer look")
        self._label(final, now)
        if chosen.resolved:
            self.pending.append((self.save(chosen, "resolved"), now, self.last_clean))
        elif counting and final not in expect:
            # A running countdown that jumped, and two frames agreed on it:
            # a real jump, or a valid glyph misread the same way twice (the
            # DR7 read 26 as 28 like that). Kept unlabeled for a look - a read
            # like this never labels itself.
            meta_stem = self.save(chosen, "surprise")
            meta = self.meta(meta_stem)
            if meta is not None:
                meta["candidates"] = sorted(expect)
                meta["label_note"] = "the countdown jumped - label by hand if it was a misread"
                self.write(meta_stem, meta)
        elif final in expect and final.strip(" -") \
                and now - self.sampled.get(final, 0.0) >= SAMPLE_EVERY:
            # Only a read the countdown agrees with labels itself: a valid
            # glyph misread the same way twice would otherwise teach the
            # check and the fit the wrong answer.
            self.sampled[final] = now
            self.save(chosen, "sample", label=final, source="read")
        self.last_clean = final

    def _label(self, after, now):
        """Label the frames waiting for a clean read, now that one came.

        A countdown's misread shows the read before it or the one after; of
        those two, the one its squarely-read segments agree with - clearly
        better than the other - is the label. Too long a gap, or no clear
        winner, and the frame keeps a null label (and its candidates).
        """
        for stem, saved, before in self.pending:
            meta = self.meta(stem)
            if meta is None:
                continue
            options = list(dict.fromkeys(t for t in (before, after) if t))
            if now - saved > LABEL_WITHIN:
                meta["label_note"] = f"no clean read within {LABEL_WITHIN} s"
            elif before and after not in expected(before):
                # More than a tick apart (a jump, or misreads in between):
                # the frame may show neither.
                meta["label_note"] = "the reads either side are more than a minute apart"
                meta["candidates"] = options
            else:
                scored = sorted((w, t) for t, w in ((t, wrong_side(meta, t)) for t in options)
                                if w is not None)
                if scored and (len(scored) == 1 or scored[1][0] > 2 * scored[0][0] + 1):
                    meta["label"], meta["source"] = scored[0][1], "reads either side"
                else:
                    meta["label_note"] = "the reads either side fit equally well"
                meta["candidates"] = options
            self.write(stem, meta)
        self.pending = []

    def save(self, cap, kind, label=None, source=None):
        os.makedirs(self.folder, exist_ok=True)
        # Names sort oldest first (prune relies on it): to the millisecond,
        # stepped on if two frames land in the same one.
        now = time.time()
        ms = int(now * 1000) % 1000
        while True:
            stem = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f".{ms:03d}-{kind}"
            if not os.path.exists(os.path.join(self.folder, stem + ".jpg")) or ms >= 999:
                break
            ms += 1
        with open(os.path.join(self.folder, stem + ".jpg"), "wb") as fh:
            fh.write(cap.jpeg)
        self.write(stem, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "machine": self.name,
            "kind": kind, "read": cap.text, "literal": cap.literal,
            "label": label, "source": source, "resolved": cap.resolved,
            "values": cap.values, "cuts": cap.cuts, "band": cap.band,
            "src": cap.src, "offset": list(cap.offset)})
        self.prune()
        log.debug("[%s] Saved frame %s (%r)", self.name, stem, cap.text)
        return stem

    def meta(self, stem):
        try:
            with open(os.path.join(self.folder, stem + ".json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def write(self, stem, meta):
        tmp = os.path.join(self.folder, stem + ".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(meta, fh, indent=1)
        os.replace(tmp, os.path.join(self.folder, stem + ".json"))

    def prune(self):
        """Keep at most `keep` frames, oldest dropped first - samples before
        anything else while they fill more than half: a running countdown
        saves one per minute, and the misreads are the frames that matter."""
        frames = sorted(f[:-4] for f in os.listdir(self.folder) if f.endswith(".jpg"))
        samples = [f for f in frames if f.endswith("-sample")]
        drop = []
        while len(frames) - len(drop) > self.keep:
            if samples and len(samples) > self.keep // 2:
                drop.append(samples.pop(0))
            else:
                drop.append(next(f for f in frames if f not in drop))
                if drop[-1] in samples:
                    samples.remove(drop[-1])
        for stem in drop:
            for path in (stem + ".jpg", stem + ".json"):
                try:
                    os.remove(os.path.join(self.folder, path))
                except OSError:
                    pass

    def summary(self):
        """Counts by kind, and how many carry a label - for the API."""
        kinds, labeled = {}, 0
        for stem, meta in saved_frames(self.folder):
            kinds[meta.get("kind", "?")] = kinds.get(meta.get("kind", "?"), 0) + 1
            labeled += bool(meta.get("label"))
        return {"folder": self.folder, "keep": self.keep, "kinds": kinds,
                "frames": sum(kinds.values()), "labeled": labeled}


def saved_frames(folder):
    """(stem, meta) of every saved frame in `folder`, oldest first."""
    try:
        names = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    except OSError:
        return []
    out = []
    for name in names:
        try:
            with open(os.path.join(folder, name)) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if os.path.exists(os.path.join(folder, name[:-5] + ".jpg")):
            out.append((name[:-5], meta))
    return out


def frame_shift(cal, meta, size):
    """Where the panel sat in a saved frame relative to `cal`, in camera px.

    The frame saved where the calibration's corners were when it was read
    (`src`); the difference from this calibration's corners is the shift to
    read it with - right after a hand-shifted or re-clicked calibration as much
    as after a realignment."""
    src = meta.get("src")
    if src and cal.warp and len(src) == 4:
        dx = sum(a[0] - b[0] for a, b in zip(src, cal.warp["src"])) / 4.0
        dy = sum(a[1] - b[1] for a, b in zip(src, cal.warp["src"])) / 4.0
    elif not cal.warp:
        dx, dy = (meta.get("offset") or (0, 0))[:2]
    else:
        dx = dy = 0
    return int(round(dx)), int(round(dy))


def labeled_planes(cal, folder, exclusive=(), name=""):
    """Every labeled saved frame, straightened for `cal`: a list of
    (stem, meta, reader-after-sampling) - the reader holds its values and cuts."""
    out = []
    for stem, meta in saved_frames(folder):
        label = meta.get("label")
        if not label or len(label) != len(cal.digits):
            continue
        try:
            image = Image.open(os.path.join(folder, stem + ".jpg")).convert("RGB")
        except OSError:
            continue
        size = tuple(cal.size) if cal.size and cal.size[0] else image.size
        reader = Reader(cal, None, exclusive, name)
        dx, dy = frame_shift(cal, meta, size)
        reader.offset = (dx, dy)
        reader.warp = shifted_warp(cal.warp, dx, dy, size)
        straight = straighten(image, reader.warp)
        reader._sample(straight, resolve=True)
        out.append((stem, meta, reader, straight))
    return out


def check_frames(cal, folder, exclusive=(), name=""):
    """Read every labeled saved frame with `cal` and report how it did.

    "read" counts frames whose segments alone read the label; "resolved" those
    that read it once a borderline segment is resolved. Per segment, the
    narrowest margin by which it read the right way when lit and when dark
    (None: no example yet), so a check also says which segments have never
    been seen lit or dark."""
    n_seg = len(SEG_ORDER)
    rows, misread, segs = [], [], {}
    for stem, meta, reader, _ in labeled_planes(cal, folder, exclusive, name):
        label = meta["label"]
        margins = segment_margins(reader.values, reader.cuts, label)
        if margins is None:
            continue
        want = "".join(GLYPH_BITS[ch] for ch in label)
        for k, (m, bit) in enumerate(zip(margins, want)):
            key = f"digit {k // n_seg + 1} {SEG_ORDER[k % n_seg]}"
            slot = segs.setdefault(key, {"lit": None, "dark": None})
            side = "lit" if bit == "1" else "dark"
            if slot[side] is None or m < slot[side][0]:
                slot[side] = (round(m, 1), stem)
        text = reader_text(reader)
        row = {"frame": stem, "label": label, "read": reader.literal, "resolved": text,
               "weakest": round(min(margins), 1)}
        rows.append(row)
        if reader.literal != label:
            misread.append(row)
    weakest = min(((s[side][0], key, side, s[side][1]) for key, s in segs.items()
                   for side in ("lit", "dark") if s[side]), default=None)
    return {
        "frames": len(rows),
        "read": sum(r["read"] == r["label"] for r in rows),
        "resolved": sum(r["resolved"] == r["label"] for r in rows),
        "misread": misread,
        "weakest": ({"segment": weakest[1], "when": weakest[2], "margin": weakest[0],
                     "frame": weakest[3]} if weakest else None),
        "never_lit": sorted(k for k, s in segs.items() if s["lit"] is None),
        "never_dark": sorted(k for k, s in segs.items() if s["dark"] is None),
        "segments": segs,
    }


def reader_text(reader):
    """The display a Reader's last _sample() resolved to."""
    text = list(reader.literal)
    for r in reader.resolved:
        text[r["digit"]] = r["as"]
    return "".join(text)


def check_line(report):
    """One log line for a check_frames report."""
    if not report["frames"]:
        return "no labeled frames saved yet"
    line = f"{report['read']} of {report['frames']} saved frames read right"
    if report["resolved"] != report["read"]:
        line += f" ({report['resolved']} with a borderline segment resolved)"
    if report["weakest"]:
        w = report["weakest"]
        line += f", weakest {w['segment']} {w['when']} ({w['margin']:+.0f} levels)"
    if report["misread"]:
        line += "; misread: " + ", ".join(f"{r['label']!r} as {r['read']!r} ({r['frame']})"
                                          for r in report["misread"][:4])
    if report["never_lit"]:
        line += "; not seen lit yet: " + ", ".join(report["never_lit"])
    if report["never_dark"]:
        line += "; not seen dark yet: " + ", ".join(report["never_dark"])
    return line


def fit_digits(data, folder, exclusive=(), name=""):
    """Lean each digit, then move each segment box, to read the saved frames
    best.

    `data` is a calibration as JSON data; returns (new data, moves). First
    each digit's slant is tried over FIT_SLANTS degrees and kept where its
    seven boxes read most squarely (no frame worse, FIT_MIN_GAIN better). Only a
    segment with saved examples both lit and dark is moved - one only ever
    seen lit would drift to the brightest spot nearby, or off its bar if only
    ever dark. Each is tried up to FIT_REACH px either way and moves where its
    narrowest margin against the cut is widest, if that gains FIT_MIN_GAIN
    levels and no frame reads worse. Nothing is saved: the result is a
    proposal for the calibration tool or the save endpoint.
    """
    import copy
    data = copy.deepcopy(data)
    cal = Calibration(data=data, quiet=True)
    frames = labeled_planes(cal, folder, exclusive, name)
    digits = sorted(data["digits"], key=lambda d: d["index"])
    n_seg = len(SEG_ORDER)
    specs = [d["segments"][s] for d in digits for s in SEG_ORDER]
    thr = [spec["threshold"] for spec in specs]
    planes, wants, vals = [], [], []
    for _, meta, reader, straight in frames:
        planes.append(channel_plane(straight, cal.channel))
        wants.append("".join(GLYPH_BITS[ch] for ch in meta["label"]))
        vals.append(list(reader.values))
    moves = []
    if not planes:
        return data, moves
    spreads = sorted(max(v) - min(v) for v in vals)
    weak = FIT_WEAK * spreads[len(spreads) // 2]
    leans = [cal.slants.get(d["index"], 0.0) for d in digits]

    def judge_digit(i, columns):
        """(frames read right, digit i's segments' narrowest margins summed,
        each capped at `weak`) with its seven segments reading `columns`."""
        right, worst = 0, [float("inf")] * n_seg
        for f, v in enumerate(vals):
            row = list(v)
            row[i * n_seg:(i + 1) * n_seg] = [columns[j][f] for j in range(n_seg)]
            cuts = segment_cuts(row, thr, cal.digit_ratio)
            right += all((x >= c) == (b == "1") for x, c, b in zip(row, cuts, wants[f]))
            for j in range(n_seg):
                k = i * n_seg + j
                m = (row[k] - cuts[k]) if wants[f][k] == "1" else (cuts[k] - row[k])
                worst[j] = min(worst[j], m)
        return right, sum(min(m, weak) for m in worst)

    # Each digit's lean first: the slant that reads its seven boxes most
    # squarely, where they sit now.
    for i, digit in enumerate(digits):
        span = range(i * n_seg, (i + 1) * n_seg)
        was = math.degrees(math.atan(leans[i]))
        base = judge_digit(i, [[v[k] for v in vals] for k in span])
        best, best_deg, best_cols = base, was, None
        for deg in FIT_SLANTS:
            if abs(deg - was) < 0.5:
                continue
            t = math.tan(math.radians(deg))
            cols = [[rect_median(p, tuple(specs[k]["rect"]), slant=t) for p in planes] for k in span]
            score = judge_digit(i, cols)
            if score > best:
                best, best_deg, best_cols = score, deg, cols
        if best_cols and (best[0] > base[0] or best[1] >= base[1] + FIT_MIN_GAIN):
            digit["slant"] = best_deg
            leans[i] = math.tan(math.radians(best_deg))
            for j, k in enumerate(span):
                for f in range(len(vals)):
                    vals[f][k] = best_cols[j][f]
            moves.append({"segment": f"digit {i + 1} slant", "by": [round(was, 1), best_deg],
                          "margin_before": round(base[1], 1), "margin_after": round(best[1], 1)})

    def judge(k, column):
        """(frames read right, segment k's narrowest margin) with segment k
        reading `column` (one level per frame)."""
        right, worst = 0, float("inf")
        for f, v in enumerate(vals):
            row = v[:k] + [column[f]] + v[k + 1:]
            cuts = segment_cuts(row, thr, cal.digit_ratio)
            ok = all((x >= c) == (b == "1") for x, c, b in zip(row, cuts, wants[f]))
            right += ok
            m = (row[k] - cuts[k]) if wants[f][k] == "1" else (cuts[k] - row[k])
            worst = min(worst, m)
        return right, worst

    for _ in range(2):
        for k, spec in enumerate(specs):
            states = {w[k] for w in wants}
            if states != {"0", "1"}:
                continue
            x, y, w, h = spec["rect"]
            base = judge(k, [v[k] for v in vals])
            if base[0] == len(vals) and base[1] >= weak:
                continue
            best, best_at = base, (0, 0)
            for dy in range(-FIT_REACH, FIT_REACH + 1):
                for dx in range(-FIT_REACH, FIT_REACH + 1):
                    if not dx and not dy:
                        continue
                    column = [rect_median(p, (x + dx, y + dy, w, h), slant=leans[k // n_seg])
                              for p in planes]
                    score = judge(k, column)
                    if (score[0], score[1]) > (best[0], best[1]) or (
                            score == best and abs(dx) + abs(dy) < sum(map(abs, best_at))):
                        best, best_at = score, (dx, dy)
            if best_at != (0, 0) and best[0] >= base[0] and (
                    best[0] > base[0] or best[1] >= base[1] + FIT_MIN_GAIN):
                dx, dy = best_at
                spec["rect"] = [x + dx, y + dy, w, h]
                for f, p in enumerate(planes):
                    vals[f][k] = rect_median(p, tuple(spec["rect"]), slant=leans[k // n_seg])
                key = f"digit {k // n_seg + 1} {SEG_ORDER[k % n_seg]}"
                done = next((m for m in moves if m["segment"] == key), None)
                if done:                     # moved again on the second pass
                    done["by"] = [done["by"][0] + dx, done["by"][1] + dy]
                    done["margin_after"] = round(best[1], 1)
                else:
                    moves.append({"segment": key, "by": [dx, dy],
                                  "margin_before": round(base[1], 1),
                                  "margin_after": round(best[1], 1)})
    return data, moves


class Machine:
    """One physical machine: its profile, camera, topics and schedule."""

    def __init__(self, spec):
        kind = str(spec.get("type", "")).strip().lower()
        if kind not in PROFILES:
            raise ValueError(
                f"Unknown machine type {kind!r} — known types: "
                f"{', '.join(sorted(PROFILES))}")
        self.type = kind
        self.profile = PROFILES[kind]
        # The id is the whole MQTT and entity identity. Defaulting it to the
        # machine type keeps a single DR7's topics and unique_ids byte for
        # byte what 0.1.x published, so its entities survive the rename.
        self.id = str(spec.get("id") or kind).strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]+", self.id):
            raise ValueError(
                f"Machine id {self.id!r} must be letters, digits, "
                f"underscores or hyphens — it becomes an MQTT topic")
        self.name = spec.get("name") or self.profile["name"]
        if not spec.get("calibration_path"):
            raise ValueError(f"Machine {self.id!r} has no calibration_path")
        # The picture comes from a camera URL or from a Home Assistant camera
        # entity (fetched live through HA, so every read and every "Grab frame"
        # is current) - exactly one of the two.
        url, entity = spec.get("snapshot_url"), spec.get("camera_entity")
        if bool(url) == bool(entity):
            raise ValueError(f"Machine {self.id!r} needs exactly one of "
                             f"snapshot_url or camera_entity")
        if entity:
            if not re.fullmatch(r"camera\.[a-z0-9_]+", entity):
                raise ValueError(f"Machine {self.id!r}: camera_entity {entity!r} "
                                 f"must be a camera entity id like camera.washer_panel")
            url = camera_url(entity)
        self.camera_entity = entity or None
        self.url = url
        self.cal_path = spec["calibration_path"]
        self.poll_active = int(spec.get("poll_active", 10))
        self.poll_idle = int(spec.get("poll_idle", 60))
        # Frames worth keeping (see FrameStore), beside the calibration file.
        # The countdown the reader had seen (last clean read, last tick),
        # handed to the next Reader when the calibration reloads: on a real
        # DR7 a reload mid-cycle forgot it, and 26 misread as 28 stood.
        self.memory = (None, None)
        self.frames = FrameStore(
            os.path.join(os.path.dirname(os.path.abspath(self.cal_path)), "frames", self.id),
            spec.get("keep_frames", KEEP_FRAMES), self.id)
        self.last_check = None

        base = f"{self.id}/panel"
        self.state_topic = f"{base}/state"
        self.avail_topic = f"{base}/availability"
        self.debug_topic = f"{base}/debug"
        self.uid = f"speedqueen_{self.id}"
        self.device = {
            "identifiers": [self.uid],
            "name": self.name,
            "manufacturer": "Speed Queen",
            "model": self.profile["model"],
        }

        self.reader = None
        self.cal_mtime = None
        self.announced = False
        self.due = 0.0
        self.warned_missing = False

    def ensure_reader(self):
        """Load calibration on first use, and not before it exists.

        A file changed on disk — edited by hand rather than saved from the
        calibration UI — is reloaded on the next poll, the same reset a UI save
        does, so the new regions take effect without restarting the add-on.
        """
        if self.reader is not None:
            try:
                mtime = os.path.getmtime(self.cal_path)
            except OSError:
                return True          # file gone: keep decoding with what is loaded
            if mtime == self.cal_mtime:
                return True
            log.info("[%s] Calibration file changed on disk — reloading", self.id)
            self.reader = None
            self.announced = False
        if not os.path.exists(self.cal_path):
            if not self.warned_missing:
                log.error("[%s] Calibration file not found at %s — waiting",
                          self.id, self.cal_path)
                self.warned_missing = True
            return False
        cal = Calibration(self.cal_path)
        if cal.machine and cal.machine != self.type:
            raise ValueError(
                f"Calibration at {self.cal_path} was built for "
                f"{cal.machine!r} but this machine is configured as "
                f"{self.type!r}")
        self.validate(cal)
        want = self.profile["channel"]
        if cal.channel != want:
            log.warning(
                "[%s] Calibration samples the %s channel but the %s decodes "
                "reliably on %s — re-export from the tool unless you know "
                "this panel differs",
                self.id, cal.channel, self.profile["model"], want)
        self.reader = Reader(cal, self.url, self.profile["exclusive"], self.id)
        self.reader.last_clean, self.reader.ticked_at = self.memory
        self.cal_mtime = os.path.getmtime(self.cal_path)
        self.warned_missing = False
        log.info("[%s] %s: %d indicators, %d digits, channel %s",
                 self.id, self.profile["model"], len(cal.leds),
                 len(cal.digits), cal.channel)
        self.check(cal)
        return True

    def check(self, cal, quiet=False):
        """Check a calibration against this machine's saved frames, log the
        result (unless quiet: an API request), and return the report. Never
        fatal - it is advice."""
        try:
            report = check_frames(cal, self.frames.folder, self.profile["exclusive"], self.id)
        except Exception:                   # noqa: BLE001 - a check must not stop the reads
            log.exception("[%s] Calibration check failed", self.id)
            return None
        if report["frames"] and not quiet:
            log.info("[%s] Calibration check: %s", self.id, check_line(report))
        if cal is getattr(self.reader, "cal", None):
            self.last_check = report
        return report

    def validate(self, cal):
        """Catch name collisions before they silently overwrite a payload key."""
        exclusive = set(self.profile["exclusive"])
        seen = set()
        for led in cal.leds:
            name = led["name"]
            if name in RESERVED or name in exclusive:
                raise ValueError(
                    f"Indicator {name!r} collides with a reserved payload key")
            if name in seen:
                raise ValueError(f"Duplicate indicator {name!r}")
            seen.add(name)

    def availability(self):
        return [{"topic": ADDON_AVAIL_TOPIC}, {"topic": self.avail_topic}]

    def discovery_payloads(self, leds):
        items = []
        profile = self.profile

        def base(uid):
            return {
                "device": self.device,
                "availability": self.availability(),
                "availability_mode": "all",
                "state_topic": self.state_topic,
                "unique_id": f"{self.uid}_{uid}",
            }

        items.append(("sensor", "display", {
            **base("display"), "name": "Display",
            "value_template": "{{ value_json.display }}",
            "icon": "mdi:counter"}))

        items.append(("sensor", "time_remaining", {
            **base("time_remaining"), "name": "Time remaining",
            "value_template": "{{ value_json.time_remaining | default('unknown', true) }}",
            "unit_of_measurement": "min", "device_class": "duration",
            "state_class": "measurement"}))

        items.append(("sensor", "state", {
            **base("state"), "name": "State",
            "value_template": "{{ value_json.state }}",
            "icon": profile["icon"]}))

        for group in profile["exclusive"]:
            items.append(("sensor", group, {
                **base(group),
                "name": label_for(profile, {}, group),
                "value_template": "{{ value_json.%s }}" % group,
                "icon": profile["group_icons"].get(group, "mdi:label")}))

        for name, info in leds.items():
            if info["group"] in profile["exclusive"]:
                continue
            cfg = {
                **base(name), "name": label_for(profile, leds, name),
                "value_template": "{{ value_json.%s }}" % name,
                "payload_on": "ON", "payload_off": "OFF",
            }
            if name in profile["device_class"]:
                cfg["device_class"] = profile["device_class"][name]
            items.append(("binary_sensor", name, cfg))

        items.append(("sensor", "reader", {
            **base("reader"), "name": "Reader",
            "value_template": "{{ value_json.reader | default('ok') }}",
            "device_class": "enum", "options": list(READER_STATES),
            "icon": "mdi:eye-check-outline", "entity_category": "diagnostic"}))

        items.append(("binary_sensor", "decode_problem", {
            **base("decode_problem"), "name": "Decode problem",
            "value_template": "{{ value_json.decode_problem }}",
            "payload_on": "ON", "payload_off": "OFF",
            "device_class": "problem", "entity_category": "diagnostic"}))

        return items

    def announce(self, client, leds, debug_image):
        for kind, uid, cfg in self.discovery_payloads(leds):
            client.publish(f"{DISCOVERY}/{kind}/{self.uid}/{uid}/config",
                           json.dumps(cfg), retain=True)
        if debug_image:
            client.publish(
                f"{DISCOVERY}/camera/{self.uid}/panel/config",
                json.dumps({
                    "device": self.device, "name": "Panel",
                    "unique_id": f"{self.uid}_panel_image",
                    "topic": self.debug_topic,
                    "availability": self.availability(),
                    "availability_mode": "all",
                    "entity_category": "diagnostic"}),
                retain=True)
        self.announced = True

    def poll(self, client, debug_image):
        """One read-and-publish cycle. Returns the next interval in seconds."""
        if not self.ensure_reader():
            return self.poll_idle

        try:
            img, leds, text, anchor_ok, overlay = self.reader.read()
        except (urllib.error.URLError, OSError) as exc:
            log.warning("[%s] Snapshot failed: %s", self.id, exc)
            client.publish(self.avail_topic, "offline", retain=True)
            return self.poll_idle

        if not self.announced:
            self.announce(client, leds, debug_image)

        payload = interpret(self.profile, leds, text)
        crowded = crowded_groups(self.profile, leds)
        if crowded:
            log.debug("[%s] implausible frame (view blocked?): %s",
                      self.id, ", ".join(crowded))
        payload["decode_problem"] = ("OFF" if anchor_ok and "?" not in text
                                     and not crowded else "ON")
        payload["reader"] = ("hidden" if crowded else
                             "misread" if not anchor_ok or "?" in text else "ok")
        chosen = self.reader.chosen
        payload["raw"] = chosen.literal if chosen is not None else text
        self.frames.record(self.reader.captures, chosen, self.reader.expect,
                           self.reader.counting)
        self.memory = (self.reader.last_clean, self.reader.ticked_at)

        client.publish(self.avail_topic, "online", retain=True)
        client.publish(self.state_topic, json.dumps(payload), retain=True)
        if debug_image:
            client.publish(self.debug_topic, annotate(img, overlay, text),
                           retain=True)

        log.debug("[%s] state=%s display=%r", self.id, payload["state"], text)
        return self.poll_active if payload["active"] else self.poll_idle


# ---------------------------------------------------------------------------
# Calibration UI
#
# Served over Home Assistant ingress, which means Supervisor handles the
# authentication and nothing is exposed outside Home Assistant. The page is
# the same single file you can open from `file://` — it grows two buttons
# when it finds this API answering, and behaves exactly as before when it
# doesn't.
#
# The snapshot proxy is not a convenience. ESPHome's camera serves its
# snapshot without an Access-Control-Allow-Origin header, so a browser cannot
# read those pixels directly: fetch is refused outright, and drawing the image
# taints the canvas, which is what getImageData needs. Fetching server-side
# and handing the bytes back on this origin is the only way the page can
# sample a frame it pulled itself. It also sidesteps a Home Assistant on
# https being unable to touch a camera on http.
# ---------------------------------------------------------------------------


def write_calibration(machine, raw):
    """Validate an uploaded calibration and put it where the machine expects.

    Returns an error string, or None when it was written. The destination is
    always the path from the add-on's own configuration — never anything the
    browser sent — so a request can only overwrite a file the add-on was
    already pointed at.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return f"not valid JSON: {exc}"
    if not isinstance(data, dict):
        return "expected a JSON object"
    if data.get("version") != 1:
        return f"unsupported calibration version {data.get('version')!r}"
    if data.get("machine") and data["machine"] != machine.type:
        return (f"calibration was built for {data['machine']!r}, "
                f"not {machine.type!r}")
    if not data.get("leds") or not data.get("digits"):
        return "calibration has no indicators or no digits"
    try:
        parse_warp(data.get("warp"))
        parse_digit_ratio(data.get("digit_ratio"))
        parse_slant(data.get("digit_slant"))
        for d in data["digits"]:
            if isinstance(d, dict):
                parse_slant(d.get("slant"), f"digit {d.get('index')} slant")
    except ValueError as exc:
        return str(exc)

    path = machine.cal_path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)

    # Pick the new file up on the next poll, and re-announce discovery in case
    # the indicator set changed. The poll loop only ever reads these, so the
    # worst a race costs is one cycle.
    machine.reader = None
    machine.announced = False
    machine.warned_missing = False
    log.info("[%s] Calibration saved to %s (%d indicators)",
             machine.id, path, len(data["leds"]))
    return None


def make_handler(machines, tool_path):
    index = {m.id: m for m in machines}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "sq-panel"

        def log_message(self, fmt, *args):
            log.debug("ui %s", fmt % args)

        def reply(self, code, body=b"", ctype="text/plain; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def reply_json(self, code, payload):
            self.reply(code, json.dumps(payload), "application/json")

        def route(self):
            return urllib.parse.urlparse(self.path).path.strip("/")

        def machine_from(self, prefix):
            """The configured machine named by the path, or None.

            Lookup is by exact id against the configured set, so a path that
            tries to escape simply does not match anything.
            """
            return index.get(self.route()[len(prefix):])

        def do_GET(self):
            route = self.route()
            if route in ("", "index.html"):
                try:
                    with open(tool_path, "rb") as fh:
                        return self.reply(200, fh.read(),
                                          "text/html; charset=utf-8")
                except OSError as exc:
                    return self.reply(500, f"calibration tool missing: {exc}")

            if route == "api/machines":
                return self.reply_json(200, [{
                    "id": m.id,
                    "type": m.type,
                    "model": m.profile["model"],
                    "name": m.name,
                    "channel": m.profile["channel"],
                    "calibration_path": m.cal_path,
                    "has_calibration": os.path.exists(m.cal_path),
                } for m in machines])

            if route.startswith("api/snapshot/"):
                machine = self.machine_from("api/snapshot/")
                if machine is None:
                    return self.reply(404, "no such machine")
                try:
                    raw = fetch(machine.url)
                except (urllib.error.URLError, OSError) as exc:
                    return self.reply(502, f"camera unreachable: {exc}")
                return self.reply(200, raw, "image/jpeg")

            # Saved frames (FrameStore): what is kept, and the check of the
            # loaded calibration against them.
            if route.startswith("api/frames/"):
                machine = self.machine_from("api/frames/")
                if machine is None:
                    return self.reply(404, "no such machine")
                return self.reply_json(200, {**machine.frames.summary(),
                                             "check": machine.last_check})

            # Check the calibration file as it is on disk now.
            if route.startswith("api/check/"):
                machine = self.machine_from("api/check/")
                if machine is None:
                    return self.reply(404, "no such machine")
                try:
                    cal = Calibration(machine.cal_path, quiet=True)
                except (OSError, ValueError, KeyError) as exc:
                    return self.reply(400, f"calibration unreadable: {exc}")
                return self.reply_json(200, machine.check(cal, quiet=True))

            # Propose digit boxes fitted to the saved frames. Saves nothing.
            if route.startswith("api/fit/"):
                machine = self.machine_from("api/fit/")
                if machine is None:
                    return self.reply(404, "no such machine")
                try:
                    with open(machine.cal_path) as fh:
                        data = json.load(fh)
                    fitted, moves = fit_digits(data, machine.frames.folder,
                                               machine.profile["exclusive"], machine.id)
                    before = machine.check(Calibration(data=data, quiet=True), quiet=True)
                    after = machine.check(Calibration(data=fitted, quiet=True), quiet=True)
                except (OSError, ValueError, KeyError) as exc:
                    return self.reply(400, f"calibration unreadable: {exc}")
                return self.reply_json(200, {"moves": moves, "before": before,
                                             "after": after, "calibration": fitted})

            return self.reply(404, "not found")

        def do_POST(self):
            route = self.route()
            prefix = next((p for p in ("api/calibration/", "api/check/")
                           if route.startswith(p)), None)
            if prefix is None:
                return self.reply(404, "not found")
            machine = self.machine_from(prefix)
            if machine is None:
                return self.reply(404, "no such machine")
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return self.reply(400, "bad Content-Length")
            if length <= 0:
                return self.reply(400, "empty upload")
            if length > MAX_UPLOAD:
                return self.reply(413, "calibration too large")
            raw = self.rfile.read(length)
            if prefix == "api/check/":
                # Check a calibration against the saved frames without saving it.
                try:
                    data = json.loads(raw)
                    if data.get("machine") and data["machine"] != machine.type:
                        raise ValueError(f"calibration was built for {data['machine']!r}")
                    cal = Calibration(data=data, quiet=True)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    return self.reply(400, f"not a calibration: {exc}")
                return self.reply_json(200, machine.check(cal, quiet=True))
            problem = write_calibration(machine, raw)
            if problem:
                log.warning("[%s] Rejected calibration upload: %s",
                            machine.id, problem)
                return self.reply(400, problem)
            return self.reply_json(200, {"saved": machine.cal_path})

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve_calibration_ui(machines, port=INGRESS_PORT, tool_path=TOOL_PATH):
    """Start the ingress UI. Never fatal — the decoder matters more."""
    try:
        httpd = Server(("0.0.0.0", port), make_handler(machines, tool_path))
    except OSError as exc:
        log.warning("Calibration UI unavailable on port %d: %s", port, exc)
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="calibration-ui").start()
    log.info("Calibration UI listening on :%d", port)
    return httpd


def build_machines(raw):
    specs = json.loads(raw) if raw else []
    if isinstance(specs, dict):
        specs = [specs]
    if not specs:
        raise ValueError("No machines configured")
    machines = []
    for spec in specs:
        machine = Machine(spec)
        if any(m.id == machine.id for m in machines):
            raise ValueError(
                f"Two machines share the id {machine.id!r} — give one an "
                f"explicit distinct 'id'")
        machines.append(machine)
    return machines


def main():
    logging.basicConfig(
        level=getattr(logging, env("LOG_LEVEL", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")

    debug_image = env("PUBLISH_DEBUG_IMAGE", True, bool)
    machines = build_machines(env("MACHINES"))
    log.info("Configured machines: %s",
             ", ".join(f"{m.id} ({m.profile['model']})" for m in machines))

    serve_calibration_ui(machines)

    client = mqtt.Client(client_id="sq-panel")
    user, password = env("MQTT_USER"), env("MQTT_PASSWORD")
    if user:
        client.username_pw_set(user, password)
    client.will_set(ADDON_AVAIL_TOPIC, "offline", retain=True)
    client.connect(env("MQTT_HOST", "core-mosquitto"), env("MQTT_PORT", 1883, int), 60)
    client.loop_start()
    client.publish(ADDON_AVAIL_TOPIC, "online", retain=True)

    running = {"go": True}
    signal.signal(signal.SIGTERM, lambda *_: running.update(go=False))
    signal.signal(signal.SIGINT, lambda *_: running.update(go=False))

    while running["go"]:
        now = time.monotonic()
        for machine in machines:
            if machine.due > now:
                continue
            try:
                interval = machine.poll(client, debug_image)
            except (ValueError, KeyError) as exc:
                # Bad calibration file or legend — a standing condition, so
                # don't bury the log in identical tracebacks every cycle.
                log.error("[%s] %s", machine.id, exc)
                interval = machine.poll_idle
            except Exception:
                log.exception("[%s] Decode failed", machine.id)
                interval = machine.poll_idle
            machine.due = time.monotonic() + max(1.0, interval)

        nap = min((m.due - time.monotonic() for m in machines), default=1.0)
        time.sleep(min(5.0, max(0.2, nap)))

    for machine in machines:
        client.publish(machine.avail_topic, "offline", retain=True)
    client.publish(ADDON_AVAIL_TOPIC, "offline", retain=True)
    client.loop_stop()


if __name__ == "__main__":
    main()
