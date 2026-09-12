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

import io
import json
import logging
import os
import re
import signal
import time
import urllib.error
import urllib.request

import numpy as np
import paho.mqtt.client as mqtt
from PIL import Image, ImageDraw

DISCOVERY = "homeassistant"

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
    # PROVISIONAL — the washer profile has never been checked against a real
    # TR7 panel. The indicator names, groups and labels below are a best guess
    # at the printed legend; the sampling machinery around them is the same
    # code the DR7 uses. Correct the list in tools/sq-calibrate.html (which
    # drives the click order and ships the labels) and mirror any name or
    # group change here. ROI coordinates never live here — they come from
    # calibration.json.
    "tr7": {
        "model": "TR7",
        "name": "Speed Queen TR7",
        "icon": "mdi:washing-machine",
        "exclusive": ("cycle", "soil", "temp", "spin_speed"),
        "group_icons": {
            "cycle": "mdi:format-list-bulleted",
            "soil": "mdi:liquid-spot",
            "temp": "mdi:thermometer",
            "spin_speed": "mdi:rotate-right",
        },
        "states": (
            ("done", ("complete",)),
            ("running", ("fill", "wash", "rinse", "spin")),
        ),
        "active": ("running", "ready"),
        # No "lock" device class for the lock indicators: Home Assistant
        # reads that class as on = Unlocked, which is backwards for an LED
        # that lights when the lock is engaged.
        "device_class": {
            "out_of_balance": "problem",
        },
        "labels": {
            # Group names double as the collapsed sensors' names.
            "soil": "Soil Level", "temp": "Water Temp",
            "spin_speed": "Spin Speed",
            "normal_eco": "Normal Eco", "heavy_duty": "Heavy Duty",
            "whites": "Whites", "colors": "Colors",
            "perm_press": "Perm Press", "delicates": "Delicates",
            "bulky": "Bulky", "quick_wash": "Quick Wash",
            "rinse_and_spin": "Rinse & Spin", "drain_and_spin": "Drain & Spin",
            "light": "Light", "normal": "Normal", "heavy": "Heavy",
            "hot": "Hot", "warm": "Warm", "cool": "Cool", "cold": "Cold",
            "spin_low": "Low", "spin_medium": "Medium", "spin_high": "High",
            "fill": "Fill", "wash": "Wash", "rinse": "Rinse", "spin": "Spin",
            "complete": "Complete",
            "extra_rinse": "Extra Rinse", "soak": "Soak",
            "delay_wash": "Delay Wash", "signal": "Signal",
            "favorites": "Favorites",
            "lid_lock": "Lid Lock", "control_lock": "Control Lock",
            "out_of_balance": "Out of Balance",
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
