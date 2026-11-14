"""0.10.0 checks: the light lock - lit indicators placing the panel to the pixel.

Synthetic TR7 frames with no anchors (as on the real machines, where the
printed triangles are too close together to be used): the panel is drawn a
few px from where the calibration has it, and the lit lights must find it.
"""

import io
import json
import math
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "speedqueen_panel"))
sys.path.insert(0, str(ROOT / "tests"))

import synth                                        # noqa: E402
import toollists                                    # noqa: E402
from harness import FakeClient, Suite               # noqa: E402
from PIL import Image, ImageDraw                    # noqa: E402

RUNNING = ["normal_eco", "temp_warm_cold", "load_auto_fill", "soil_heavy", "wash"]


def defs(tool):
    leds = tool["tr7"]["leds"]
    points = synth.led_grid(len(leds))
    return [(l["group"], l["name"], l["label"], points[i]) for i, l in enumerate(leds)]


def machine(decoder, tool, tmp):
    d = defs(tool)
    cal = synth.calibration("tr7", d)
    cal["anchors"] = []
    path = os.path.join(tmp, "tr7.json")
    with open(path, "w") as fh:
        json.dump(cal, fh)
    m = decoder.Machine({"type": "tr7", "snapshot_url": "http://cam.invalid/", "calibration_path": path})
    return m, cal, {n: xy for _, n, _, xy in d}


def frame(positions, lit, text, dx=0.0, dy=0.0, extra=None):
    jpeg = synth.render(set(lit), text, positions, warp=synth.Warp(dx=dx, dy=dy), machine="tr7")
    if not extra:
        return jpeg
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for x, y in extra:                              # a stray lit spot (glare) off any indicator
        draw.ellipse([x - 7, y - 7, x + 7, y + 7], fill=synth.PALETTE["tr7"]["led_lit"])
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def sample(decoder, reader, jpeg, lock=True):
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    return reader._sample(decoder.straighten(image, reader.warp), resolve=True, lock=lock)


def lit_names(leds):
    return sorted(n for n, i in leds.items() if i["on"])


def run(s=None):
    s = s or Suite("lock")
    import decoder
    real_fetch = decoder.fetch
    decoder.CONFIRM_GAP = decoder.FETCH_RETRY_DELAY = 0
    tool = toollists.machines()
    try:
        _run(s, decoder, tool)
    finally:
        decoder.fetch = real_fetch
    return s


def _run(s, decoder, tool):
    # --- locate_light: the centre of a light, not its first bright pixel ------
    import numpy as np
    plane = np.zeros((80, 100), dtype=np.float32)
    yy, xx = np.ogrid[0:80, 0:100]
    plane[(xx - 53.0) ** 2 + (yy - 36.0) ** 2 <= 49] = 240.0
    got = decoder.locate_light(plane, 47, 42, 7, 10)
    s.truthy("locate_light finds the light", got is not None)
    if got:
        s.check("locate_light centre x", round(got[0]), 53)
        s.check("locate_light centre y", round(got[1]), 36)
    s.check("locate_light: nothing within reach", decoder.locate_light(plane, 20, 60, 7, 6), None)
    s.check("locate_light: window off the frame", decoder.locate_light(plane, 3, 3, 7, 10), None)

    with tempfile.TemporaryDirectory() as tmp:
        m, cal, pos = machine(decoder, tool, tmp)
        c = decoder.Calibration(data=cal, quiet=True)
        reach = decoder.lock_reach(c.leds, c.radius)
        s.truthy("lock_reach stays inside half the closest gap", 2 <= reach <= decoder.LOCK_REACH)

        # --- a panel 6 px right, 5 px up of its calibration --------------------
        moved = frame(pos, RUNNING, "49", dx=6, dy=-5)
        r = decoder.Reader(c, None, decoder.PROFILES["tr7"]["exclusive"], "tr7")
        _, leds, text, _, _ = sample(decoder, r, moved, lock=False)
        s.truthy("unlocked: the shift misreads the lights", lit_names(leds) != sorted(RUNNING))

        r = decoder.Reader(c, None, (), "tr7")
        _, leds, text, _, overlay = sample(decoder, r, moved)
        s.check("locked: every light reads right", lit_names(leds), sorted(RUNNING))
        s.check("locked: the display reads right", text, "49")
        s.truthy("locked: the lights found the shift",
                 r.lights and abs(r.lights["dx"] - 6) <= 1 and abs(r.lights["dy"] + 5) <= 1)
        s.check("locked: all five lit lights agree", (r.lights or {}).get("marks"), 5)
        s.check("one read does not move the placement", r.offset, (0, 0))
        s.truthy("overlay notes the lights' shift", any(o[0] == "note" and "lights" in o[1] for o in overlay))

        _, leds, text, _, _ = sample(decoder, r, moved)
        s.check("two agreeing reads move the placement", r.offset, (6, -5))
        s.truthy("...and mark the reference for a refresh", r.lights_moved)
        _, leds, text, _, _ = sample(decoder, r, moved)
        s.check("placed: lights read right", lit_names(leds), sorted(RUNNING))
        s.truthy("placed: the lights now confirm the placement", r.lights_fresh() and not r.lights_disagree())
        s.check("placed: stays put", r.offset, (6, -5))

        # --- what must NOT move it ----------------------------------------------
        r = decoder.Reader(c, None, (), "tr7")
        for _ in range(3):
            sample(decoder, r, frame(pos, [], "  ", dx=6, dy=-5))
        s.check("dark panel: no lock", (r.lights, r.offset), (None, (0, 0)))

        r = decoder.Reader(c, None, (), "tr7")
        for _ in range(3):
            sample(decoder, r, frame(pos, ["normal_eco", "wash"], "49", dx=6, dy=-5))
        s.check("two lit lights: not enough to lock", (r.lights, r.offset), (None, (0, 0)))

        options = [l["name"] for l in cal["leds"] if l["group"] == "option"][:3]
        r = decoder.Reader(c, None, (), "tr7")
        for _ in range(3):
            sample(decoder, r, frame(pos, options, "49", dx=6, dy=-5))
        s.check("lights from one group only: no lock", (r.lights, r.offset), (None, (0, 0)))

        r = decoder.Reader(c, None, (), "tr7")
        for _ in range(3):
            sample(decoder, r, frame(pos, RUNNING, "49"))
        s.check("panel where calibrated: placement unchanged", r.offset, (0, 0))
        s.truthy("...and the lights confirm it", r.lights_fresh())

        # a stray bright spot beside one light is outvoted by the others
        nx, ny = pos["normal_eco"]
        r = decoder.Reader(c, None, (), "tr7")
        for _ in range(2):
            _, leds, _, _, _ = sample(decoder, r, frame(pos, RUNNING, "49", dx=6, dy=-5,
                                                        extra=[(nx + 6 - 9, ny - 5 - 8)]))
        s.check("glare beside one light: still placed by the rest", r.offset, (6, -5))

        # a move further than the lights can reach is left to the reference match
        r = decoder.Reader(c, None, (), "tr7")
        for _ in range(3):
            sample(decoder, r, frame(pos, RUNNING, "49", dx=0, dy=28))
        s.truthy("28 px move: the lights do not lock onto the next row",
                 r.offset == (0, 0) or abs(r.offset[1] - 28) <= 1)

        # --- the reference match defers to fresh lights ---------------------------
        real = decoder.register_pose
        try:
            decoder.register_pose = lambda ref, cur, pivot, turn=(0.0, 1.0), search=True: (
                decoder.pose_shift(5, 0), 0.9, 2.0, None, None)
            img = Image.open(io.BytesIO(frame(pos, RUNNING, "49"))).convert("RGB")
            r = decoder.Reader(c, None, (), "tr7")
            r.reference = (img, decoder.IDENTITY)
            sample(decoder, r, frame(pos, RUNNING, "49"))
            s.check("lights fresh: a 5 px match is not adopted", (r.register(img), r.offset), (False, (0, 0)))
            r.lights_ok_at = None
            s.check("lights stale: the same match is adopted", (r.register(img), r.offset), (True, (5, 0)))
            decoder.register_pose = lambda ref, cur, pivot, turn=(0.0, 1.0), search=True: (
                decoder.pose_shift(40, 0), 0.9, 2.0, None, None)
            r = decoder.Reader(c, None, (), "tr7")
            r.reference = (img, decoder.IDENTITY)
            sample(decoder, r, frame(pos, RUNNING, "49"))
            s.check("lights fresh: a 40 px move is still adopted", (r.register(img), r.offset), (True, (40, 0)))
        finally:
            decoder.register_pose = real

        # --- the reference picture is never saved at a placement the lights reject -----
        class Cap:
            hidden, resolved, text, jpeg = False, [], "49", b"jpeg"
            result = (None, {}, "49", True, [])

        saved = []

        class FakeMachine:
            id = "tr7"

            def reference_age(self):
                return 60.0                     # fresh: only a stale reference is replaced

            def save_reference(self, jpeg, pose, display=""):
                saved.append(display)

        r = decoder.Reader(c, None, (), "tr7")
        r.lock_hist = [(6.0, -5.0)]
        decoder.Machine.maybe_reference(FakeMachine(), r, Cap(), True)
        s.check("lights disagree: the reference is not refreshed", saved, [])
        r.lock_hist, r.lights_moved = [(0.2, 0.1)], True
        decoder.Machine.maybe_reference(FakeMachine(), r, Cap(), True)
        s.check("lights just moved the placement: refreshed at once", saved, ["49"])
        r.lights_moved = False
        decoder.Machine.maybe_reference(FakeMachine(), r, Cap(), True)
        s.check("otherwise a fresh reference is kept", saved, ["49"])

        # --- fit_lights: one light placed 4 px off is moved onto its glow -------------
        folder = os.path.join(tmp, "frames")
        os.makedirs(folder)
        loads = [(RUNNING, 0, 0), (["normal_eco", "temp_warm_cold", "soil_heavy", "wash"], 3, -2),
                 (["heavy_duty", "temp_warm_cold", "load_auto_fill", "rinse"], -2, 1),
                 (RUNNING, 1, 3)]
        for i, (lit, dx, dy) in enumerate(loads):
            with open(os.path.join(folder, f"2026111{i}-120000.000-resolved.jpg"), "wb") as fh:
                fh.write(frame(pos, lit, "49", dx=dx, dy=dy))
            with open(os.path.join(folder, f"2026111{i}-120000.000-resolved.json"), "w") as fh:
                json.dump({"kind": "resolved", "read": "49"}, fh)
        off = json.loads(json.dumps(cal))
        for led in off["leds"]:
            if led["name"] == "temp_warm_cold":
                led["x"] += 4
        fitted, moves = decoder.fit_lights(off, folder)
        s.check("fit_lights moves only the misplaced light", [mv["indicator"] for mv in moves], ["temp_warm_cold"])
        if moves:
            s.check("fit_lights moves it back 4 px", [round(v) for v in moves[0]["by"]], [-4, 0])
        s.check("fit_lights leaves the input alone",
                [l["x"] for l in off["leds"] if l["name"] == "temp_warm_cold"],
                [pos["temp_warm_cold"][0] + 4])
        s.check("fit_lights: nothing to move on a right calibration", decoder.fit_lights(cal, folder)[1], [])
        s.check("fit_lights: no frames, no moves", decoder.fit_lights(cal, os.path.join(tmp, "none"))[1], [])

        # --- end to end: polls publish the right lights and the lights' placement -----
        frames = [frame(pos, RUNNING, "49", dx=6, dy=-5)]
        decoder.fetch = lambda url, timeout=10: frames[0]
        client = FakeClient()
        m.poll(client, False)
        st = client.last_json("tr7/panel/state")
        s.check("poll 1: soil level", st["soil"], "Heavy")
        s.check("poll 1: cycle", st["cycle"], "Normal Eco")
        s.check("poll 1: wash lit", st["wash"], "ON")
        s.check("poll 1: state", st["state"], "running")
        lights = (st.get("panel") or {}).get("lights") or {}
        s.check("poll 1: lights reported", (round(lights.get("dx", 0)), round(lights.get("dy", 0)),
                                             lights.get("marks")), (6, -5, 5))
        m.poll(client, False)
        st = client.last_json("tr7/panel/state")
        s.check("poll 2: placement moved by the lights", (st["panel"]["dx"], st["panel"]["dy"]), (6, -5))
        s.check("poll 2: panel offset", st["panel_offset"], round(math.hypot(6, 5), 1))
        s.check("poll 2: soil still right", st["soil"], "Heavy")


if __name__ == "__main__":
    suite = run()
    suite.report()
    sys.exit(1 if suite.failures else 0)
