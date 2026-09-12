"""Speed Queen 7-series control panel reader.

Pulls a JPEG snapshot per machine, samples fixed regions of interest, and
publishes the result to Home Assistant over MQTT discovery.

Nothing here is learned or probabilistic. Every indicator is a brightness
threshold over a small disc; every digit is seven thresholds looked up in a
table. The only adaptive part is a two-point similarity transform that
corrects for the camera being nudged.

One add-on instance drives any number of machines. Everything machine-specific
lives in PROFILES; the sampling, transform and glyph machinery is shared
because every 7-series control uses the same two-digit seven-segment display
and the same printed triangles beside it.
"""

import http.server
import io
import json
import logging
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

# Payload keys the decoder owns. An indicator may not be named any of these,
# nor share a name with an exclusive group, or it would overwrite it.
RESERVED = {"display", "time_remaining", "state", "active", "raw",
            "decode_problem"}

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


class Calibration:
    def __init__(self, path):
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
        if len(self.anchors) != 2:
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


def locate_anchor(plane, x, y, win=26):
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


def rect_median(plane, rect, inset=0.2):
    x, y, w, h = rect
    ix, iy = w * inset, h * inset
    h_img, w_img = plane.shape
    x0, x1 = max(0, int(x + ix)), min(w_img, int(x + w - ix) + 1)
    y0, y1 = max(0, int(y + iy)), min(h_img, int(y + h - iy) + 1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    patch = plane[y0:y1, x0:x1]
    return float(np.median(patch)) if patch.size else 0.0


def fetch(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "sq-panel/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def label_for(profile, leds, name):
    """Calibration labels win, then the profile's, then the bare name."""
    entry = leds.get(name) if isinstance(leds, dict) else None
    if entry and entry.get("label"):
        return entry["label"]
    return profile["labels"].get(name, name.replace("_", " ").capitalize())


class Reader:
    def __init__(self, cal, url):
        self.cal = cal
        self.url = url

    def read(self):
        raw = fetch(self.url)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        plane = channel_plane(img, self.cal.channel)
        lum = luma_plane(img)

        tf, anchor_ok = self._align(lum)

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

        text = ""
        for digit in sorted(self.cal.digits, key=lambda d: d["index"]):
            bits = ""
            for seg in SEG_ORDER:
                spec = digit["segments"][seg]
                rx, ry, rw, rh = spec["rect"]
                px, py = tf.point(rx, ry)
                rect = (px, py, tf.length(rw), tf.length(rh))
                value = rect_median(plane, rect)
                on = value >= spec["threshold"]
                bits += "1" if on else "0"
                overlay.append(("rect", rect, on))
            text += GLYPHS.get(bits, "?")

        return img, leds, text, anchor_ok, overlay

    def _align(self, lum):
        if len(self.cal.anchors) != 2:
            return Transform(), True
        found = []
        for a in self.cal.anchors:
            pt, contrast = locate_anchor(lum, a["x"], a["y"])
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


def annotate(img, overlay, text):
    out = img.copy()
    draw = ImageDraw.Draw(out)
    lit, unlit = (91, 225, 255), (240, 160, 43)
    for item in overlay:
        if item[0] == "disc":
            _, x, y, r, on = item
            draw.ellipse([x - r, y - r, x + r, y + r],
                         outline=lit if on else unlit, width=2)
        else:
            _, (x, y, w, h), on = item
            draw.rectangle([x, y, x + w, y + h],
                           outline=lit if on else unlit, width=2)
    draw.text((12, 12), f"read: {text!r}", fill=(255, 255, 255))
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
        for key in ("snapshot_url", "calibration_path"):
            if not spec.get(key):
                raise ValueError(f"Machine {self.id!r} has no {key}")
        self.url = spec["snapshot_url"]
        self.cal_path = spec["calibration_path"]
        self.poll_active = int(spec.get("poll_active", 10))
        self.poll_idle = int(spec.get("poll_idle", 60))

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
        self.announced = False
        self.due = 0.0
        self.warned_missing = False

    def ensure_reader(self):
        """Load calibration on first use, and not before it exists."""
        if self.reader is not None:
            return True
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
        self.reader = Reader(cal, self.url)
        self.warned_missing = False
        log.info("[%s] %s: %d indicators, %d digits, channel %s",
                 self.id, self.profile["model"], len(cal.leds),
                 len(cal.digits), cal.channel)
        return True

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
        payload["decode_problem"] = "OFF" if anchor_ok and "?" not in text else "ON"
        payload["raw"] = text

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

            return self.reply(404, "not found")

        def do_POST(self):
            route = self.route()
            if not route.startswith("api/calibration/"):
                return self.reply(404, "not found")
            machine = self.machine_from("api/calibration/")
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
