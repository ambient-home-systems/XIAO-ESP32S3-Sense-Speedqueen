"""Speed Queen DR7 control panel reader.

Pulls a JPEG snapshot, samples fixed regions of interest, and publishes the
result to Home Assistant over MQTT discovery.

Nothing here is learned or probabilistic. Every indicator is a brightness
threshold over a small disc; every digit is seven thresholds looked up in a
table. The only adaptive part is a two-point similarity transform that
corrects for the camera being nudged.
"""

import io
import json
import logging
import math
import os
import signal
import time
import urllib.error
import urllib.request

import numpy as np
import paho.mqtt.client as mqtt
from PIL import Image, ImageDraw

BASE = "dr7/panel"
STATE_TOPIC = f"{BASE}/state"
AVAIL_TOPIC = f"{BASE}/availability"
DEBUG_TOPIC = f"{BASE}/debug"
DISCOVERY = "homeassistant"

DEVICE = {
    "identifiers": ["speedqueen_dr7"],
    "name": "Speed Queen DR7",
    "manufacturer": "Speed Queen",
    "model": "DR7",
}

SEG_ORDER = ("a", "b", "c", "d", "e", "f", "g")

# Seven-segment patterns in a,b,c,d,e,f,g order. Digits first, then the
# letter forms the DR7 uses for diagnostic codes.
GLYPHS = {
    "1111110": "0", "0110000": "1", "1101101": "2", "1111001": "3",
    "0110011": "4", "1011011": "5", "1011111": "6", "1110000": "7",
    "1111111": "8", "1111011": "9",
    "0000000": " ", "0000001": "-",
    "1110111": "A", "1001110": "C", "0111101": "d", "1001111": "E",
    "1000111": "F", "0110111": "H", "0001110": "L", "0010101": "n",
    "0011101": "o", "1100111": "P", "0001111": "t", "0111110": "U",
}

LABELS = {
    "heavy_duty": "Heavy Duty", "regular": "Regular", "pet_items": "Pet Items",
    "time_dry": "Time Dry", "steam_sanitize": "Steam Sanitize",
    "perm_press": "Perm Press", "delicates": "Delicates",
    "pet_hair_remover": "Pet Hair Remover", "quick_dry": "Quick Dry",
    "steam_refresh": "Steam Refresh",
    "no_heat": "No Heat", "low": "Low", "medium": "Medium", "high": "High",
    "damp": "Damp", "less_dry": "Less Dry", "near_dry": "Near Dry", "dry": "Dry",
    "sensing": "Sensing", "heating": "Heating", "cooling": "Cooling",
    "complete": "Complete",
    "extended_tumble": "Extended Tumble", "steam_boost": "Steam Boost",
    "ecodry": "EcoDry", "anti_static": "Anti-Static", "signal": "Signal",
    "favorites": "Favorites",
    "door_open": "Door Open", "control_lock": "Control Lock",
    "extended": "Extended",
}

# Groups where exactly one LED is lit at a time collapse to a single sensor.
EXCLUSIVE = {"cycle": "cycle", "temp": "temp", "dryness": "dryness"}
# Groups where any combination is possible stay as individual binary sensors.
INDEPENDENT = ("status", "option", "alert")

log = logging.getLogger("dr7")


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
    req = urllib.request.Request(url, headers={"User-Agent": "dr7-panel/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


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
                                 "group": led["group"]}
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


def interpret(leds, text):
    """Collapse raw ROI results into the published entity values."""
    out = {}
    for group in EXCLUSIVE:
        winner, best = None, -1.0
        for name, info in leds.items():
            if info["group"] == group and info["on"] and info["value"] > best:
                winner, best = name, info["value"]
        out[group] = LABELS.get(winner, "None") if winner else "None"

    for name, info in leds.items():
        if info["group"] in INDEPENDENT:
            out[name] = "ON" if info["on"] else "OFF"

    display = text.strip()
    out["display"] = display if display else "--"

    if display.isdigit():
        out["time_remaining"] = int(display)
    else:
        out["time_remaining"] = None

    fault = bool(display) and not display.isdigit() and display != "-"
    on = lambda k: out.get(k) == "ON"

    if fault:
        out["state"] = "fault"
    elif on("complete"):
        out["state"] = "done"
    elif on("cooling"):
        out["state"] = "cooling"
    elif on("sensing") or on("heating"):
        out["state"] = "running"
    elif display or out["cycle"] != "None":
        out["state"] = "ready"
    else:
        out["state"] = "off"

    out["active"] = out["state"] in ("running", "cooling", "ready")
    return out


def discovery_payloads(leds):
    items = []

    def base(uid):
        return {
            "device": DEVICE,
            "availability_topic": AVAIL_TOPIC,
            "state_topic": STATE_TOPIC,
            "unique_id": f"speedqueen_dr7_{uid}",
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
        "icon": "mdi:tumble-dryer"}))

    for group, icon in (("cycle", "mdi:format-list-bulleted"),
                        ("temp", "mdi:thermometer"),
                        ("dryness", "mdi:water-percent")):
        items.append(("sensor", group, {
            **base(group), "name": group.capitalize(),
            "value_template": "{{ value_json.%s }}" % group, "icon": icon}))

    device_class = {
        "door_open": "door", "heating": "heat", "sensing": "running",
    }
    for name, info in leds.items():
        if info["group"] not in INDEPENDENT:
            continue
        cfg = {
            **base(name), "name": LABELS.get(name, name),
            "value_template": "{{ value_json.%s }}" % name,
            "payload_on": "ON", "payload_off": "OFF",
        }
        if name in device_class:
            cfg["device_class"] = device_class[name]
        items.append(("binary_sensor", name, cfg))

    items.append(("binary_sensor", "decode_problem", {
        **base("decode_problem"), "name": "Decode problem",
        "value_template": "{{ value_json.decode_problem }}",
        "payload_on": "ON", "payload_off": "OFF",
        "device_class": "problem", "entity_category": "diagnostic"}))

    return items


def main():
    logging.basicConfig(
        level=getattr(logging, env("LOG_LEVEL", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")

    url = env("SNAPSHOT_URL", "http://dryer-cam.local:8081/")
    cal_path = env("CALIBRATION_PATH", "/homeassistant/dr7_panel/calibration.json")
    poll_active = env("POLL_ACTIVE", 10, int)
    poll_idle = env("POLL_IDLE", 60, int)
    debug_image = env("PUBLISH_DEBUG_IMAGE", True, bool)

    while not os.path.exists(cal_path):
        log.error("Calibration file not found at %s — waiting", cal_path)
        time.sleep(30)

    cal = Calibration(cal_path)
    reader = Reader(cal, url)

    client = mqtt.Client(client_id="dr7-panel")
    user, password = env("MQTT_USER"), env("MQTT_PASSWORD")
    if user:
        client.username_pw_set(user, password)
    client.will_set(AVAIL_TOPIC, "offline", retain=True)
    client.connect(env("MQTT_HOST", "core-mosquitto"), env("MQTT_PORT", 1883, int), 60)
    client.loop_start()
    client.publish(AVAIL_TOPIC, "online", retain=True)

    running = {"go": True}
    signal.signal(signal.SIGTERM, lambda *_: running.update(go=False))
    signal.signal(signal.SIGINT, lambda *_: running.update(go=False))

    announced = False
    interval = poll_idle

    while running["go"]:
        started = time.monotonic()
        try:
            img, leds, text, anchor_ok, overlay = reader.read()

            if not announced:
                for kind, uid, cfg in discovery_payloads(leds):
                    topic = f"{DISCOVERY}/{kind}/speedqueen_dr7/{uid}/config"
                    client.publish(topic, json.dumps(cfg), retain=True)
                if debug_image:
                    client.publish(
                        f"{DISCOVERY}/camera/speedqueen_dr7/panel/config",
                        json.dumps({
                            "device": DEVICE, "name": "Panel",
                            "unique_id": "speedqueen_dr7_panel_image",
                            "topic": DEBUG_TOPIC,
                            "availability_topic": AVAIL_TOPIC,
                            "entity_category": "diagnostic"}),
                        retain=True)
                announced = True

            payload = interpret(leds, text)
            payload["decode_problem"] = "OFF" if anchor_ok and "?" not in text else "ON"
            payload["raw"] = text

            client.publish(STATE_TOPIC, json.dumps(payload), retain=True)
            if debug_image:
                client.publish(DEBUG_TOPIC, annotate(img, overlay, text), retain=True)

            interval = poll_active if payload["active"] else poll_idle
            log.debug("state=%s display=%r", payload["state"], text)

        except (urllib.error.URLError, OSError) as exc:
            log.warning("Snapshot failed: %s", exc)
            client.publish(STATE_TOPIC, json.dumps({"decode_problem": "ON"}))
            interval = poll_idle
        except Exception:
            log.exception("Decode failed")
            interval = poll_idle

        time.sleep(max(1.0, interval - (time.monotonic() - started)))

    client.publish(AVAIL_TOPIC, "offline", retain=True)
    client.loop_stop()


if __name__ == "__main__":
    main()
