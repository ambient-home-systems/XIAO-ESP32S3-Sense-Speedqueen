"""Decode checks: synthetic frames in, published MQTT payloads out.

Covers the parts that are easy to break silently — the DR7's MQTT identity,
which must stay byte-for-byte what 0.1.x published or everyone's entities are
orphaned; the per-machine sampling channel; the state machine; and the
isolation between machines.
"""

import json
import os
import pathlib
import sys
import tempfile
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "speedqueen_panel"))
sys.path.insert(0, str(ROOT / "tests"))

import synth                                        # noqa: E402
import toollists                                    # noqa: E402
from harness import FakeClient, Suite               # noqa: E402


def led_defs(tool, machine):
    """Lay the machine's indicators out on the synthetic panel."""
    leds = tool[machine]["leds"]
    points = synth.led_grid(len(leds))
    return [(l["group"], l["name"], l["label"], points[i])
            for i, l in enumerate(leds)]


def build(decoder, tool, machine, tmp, **over):
    defs = led_defs(tool, machine)
    path = os.path.join(tmp, f"{machine}.json")
    with open(path, "w") as fh:
        json.dump(synth.calibration(machine, defs), fh)
    spec = {"type": machine, "snapshot_url": "http://cam.invalid/",
            "calibration_path": path}
    spec.update(over)
    return decoder.Machine(spec), {n: xy for _, n, _, xy in defs}


def poll(decoder, m, positions, lit, text, warp=None, debug=True):
    frame = synth.render(set(lit), text, positions, warp=warp, machine=m.type)
    decoder.fetch = lambda url, timeout=10: frame
    client = FakeClient()
    interval = m.poll(client, debug)
    return client, interval


def run(s=None):
    """Accepts a Suite so a caller keeps partial results if a check raises."""
    import decoder

    s = s or Suite("decode")
    tool = toollists.machines()

    with tempfile.TemporaryDirectory() as tmp:
        # ---- DR7 mid-cycle ---------------------------------------------
        dr7, pos = build(decoder, tool, "dr7", tmp)
        client, interval = poll(decoder, dr7, pos,
                                ["regular", "medium", "dry", "sensing", "heating"], "46")
        st = client.last_json("dr7/panel/state")
        s.check("dr7 cycle", st["cycle"], "Regular")
        s.check("dr7 temp", st["temp"], "Medium")
        s.check("dr7 dryness", st["dryness"], "Dry")
        s.check("dr7 display", st["display"], "46")
        s.check("dr7 time_remaining", st["time_remaining"], 46)
        s.check("dr7 state", st["state"], "running")
        s.check("dr7 active", st["active"], True)
        s.check("dr7 sensing", st["sensing"], "ON")
        s.check("dr7 heating", st["heating"], "ON")
        s.check("dr7 cooling", st["cooling"], "OFF")
        s.check("dr7 decode_problem", st["decode_problem"], "OFF")
        s.check("dr7 polls faster while running", interval, 10)

        # ---- the DR7's identity must not move from 0.1.x ---------------
        s.check("dr7 state topic", "dr7/panel/state" in client.topics(), True)
        s.check("dr7 availability topic",
                "dr7/panel/availability" in client.topics(), True)
        s.check("dr7 debug topic", "dr7/panel/debug" in client.topics(), True)
        s.check("dr7 discovery node id",
                all("/speedqueen_dr7/" in t for t in client.discovery_topics()), True)
        cfg = client.last_json("homeassistant/sensor/speedqueen_dr7/display/config")
        s.check("dr7 unique_id", cfg["unique_id"], "speedqueen_dr7_display")
        s.check("dr7 device identifiers", cfg["device"]["identifiers"],
                ["speedqueen_dr7"])
        s.check("dr7 device name", cfg["device"]["name"], "Speed Queen DR7")
        s.check("dr7 availability list", [a["topic"] for a in cfg["availability"]],
                ["speedqueen/panel/availability", "dr7/panel/availability"])
        s.check("dr7 availability mode", cfg["availability_mode"], "all")
        s.check("dr7 cycle sensor name",
                client.last_json(
                    "homeassistant/sensor/speedqueen_dr7/cycle/config")["name"],
                "Cycle")
        door = client.last_json(
            "homeassistant/binary_sensor/speedqueen_dr7/door_open/config")
        s.check("dr7 door label", door["name"], "Door Open")
        s.check("dr7 door device_class", door["device_class"], "door")
        # 31 indicators - 18 collapsed = 13 binary, + 3 collapsed sensors,
        # + display, time_remaining, state, decode_problem, + panel camera
        s.check("dr7 entity count", len(client.discovery_topics()), 13 + 3 + 4 + 1)

        # ---- DR7 states -------------------------------------------------
        client, interval = poll(decoder, dr7, pos, [], " ")
        st = client.last_json("dr7/panel/state")
        s.check("dr7 dark panel is off", st["state"], "off")
        s.check("dr7 blank display", st["display"], "--")
        s.check("dr7 no time when blank", st["time_remaining"], None)
        s.check("dr7 backs off when idle", interval, 60)

        client, _ = poll(decoder, dr7, pos, ["regular", "high"], " ")
        s.check("dr7 selected but not started is ready",
                client.last_json("dr7/panel/state")["state"], "ready")

        client, _ = poll(decoder, dr7, pos, ["complete"], " ")
        st = client.last_json("dr7/panel/state")
        s.check("dr7 complete is done", st["state"], "done")
        s.check("dr7 done is not active", st["active"], False)

        client, _ = poll(decoder, dr7, pos, ["heating", "cooling"], "12")
        s.check("dr7 cooling outranks heating",
                client.last_json("dr7/panel/state")["state"], "cooling")

        # ---- a fault code is a correct read, not a decode problem -------
        client, _ = poll(decoder, dr7, pos, ["door_open"], "nH")
        st = client.last_json("dr7/panel/state")
        s.check("dr7 fault display", st["display"], "nH")
        s.check("dr7 fault state", st["state"], "fault")
        s.check("dr7 fault has no time", st["time_remaining"], None)
        s.check("dr7 fault is not a decode problem", st["decode_problem"], "OFF")
        s.check("dr7 fault still reads indicators", st["door_open"], "ON")

        # ---- a nudged camera is corrected by the anchors ----------------
        for dx, dy, deg, scale in ((9, -6, 0.0, 1.0), (-7, 5, 1.2, 1.02)):
            warp = synth.Warp(dx=dx, dy=dy, deg=deg, scale=scale)
            client, _ = poll(decoder, dr7, pos,
                             ["regular", "medium", "dry", "heating"], "46", warp=warp)
            st = client.last_json("dr7/panel/state")
            tag = f"dr7 nudged ({dx},{dy},{deg}deg,x{scale})"
            s.check(f"{tag} display", st["display"], "46")
            s.check(f"{tag} cycle", st["cycle"], "Regular")
            s.check(f"{tag} decode_problem", st["decode_problem"], "OFF")

        # ---- an unreadable glyph does raise decode_problem --------------
        synth.GLYPH_BITS["?"] = "0000011"          # not a pattern the decoder knows
        client, _ = poll(decoder, dr7, pos, ["regular"], "?8")
        del synth.GLYPH_BITS["?"]
        st = client.last_json("dr7/panel/state")
        s.check("dr7 unknown glyph sets decode_problem", st["decode_problem"], "ON")
        s.check("dr7 unknown glyph shows in raw", "?" in st["raw"], True)

        # ---- a label in the calibration file beats the profile's --------
        defs = led_defs(tool, "dr7")
        cal = synth.calibration("dr7", defs)
        for led in cal["leds"]:
            if led["name"] == "ecodry":
                led["label"] = "Eco Dry (relabelled)"
        path = os.path.join(tmp, "relabelled.json")
        json.dump(cal, open(path, "w"))
        relabelled = decoder.Machine({"type": "dr7", "snapshot_url": "u",
                                      "calibration_path": path})
        client, _ = poll(decoder, relabelled, pos, ["ecodry"], " ")
        s.check("calibration label wins over the profile's",
                client.last_json(
                    "homeassistant/binary_sensor/speedqueen_dr7/ecodry/config")["name"],
                "Eco Dry (relabelled)")

        # ---- TR7 washer -------------------------------------------------
        tr7, wpos = build(decoder, tool, "tr7", tmp)
        client, interval = poll(decoder, tr7, wpos,
                                ["normal_eco", "temp_warm_cold", "load_auto_fill",
                                 "soil_heavy", "wash"], "49")
        st = client.last_json("tr7/panel/state")
        s.check("tr7 cycle", st["cycle"], "Normal Eco")
        s.check("tr7 water temp", st["temp"], "Warm/Cold")
        s.check("tr7 load size", st["load_size"], "Auto Fill")
        s.check("tr7 soil level", st["soil"], "Heavy")
        s.check("tr7 display", st["display"], "49")
        s.check("tr7 time_remaining", st["time_remaining"], 49)
        s.check("tr7 state", st["state"], "running")
        s.check("tr7 wash lit", st["wash"], "ON")
        s.check("tr7 rinse dark", st["rinse"], "OFF")
        s.check("tr7 decode_problem", st["decode_problem"], "OFF")
        s.check("tr7 polls faster while running", interval, 10)
        s.check("tr7 topic namespace", "tr7/panel/state" in client.topics(), True)

        # the two names that deliberately differ from the printed legend
        s.check("tr7 status Spin owns the flat key", st["spin"], "OFF")
        s.check("tr7 the Spin cycle is not a payload key", "spin_cycle" in st, False)
        c2, _ = poll(decoder, tr7, wpos, ["spin_cycle"], " ")
        s.check("tr7 the Spin cycle collapses into cycle",
                c2.last_json("tr7/panel/state")["cycle"], "Spin")
        c2, _ = poll(decoder, tr7, wpos, ["soil_medium", "load_medium"], " ")
        both = c2.last_json("tr7/panel/state")
        s.check("tr7 the two Medium rows stay independent",
                (both["soil"], both["load_size"]), ("Medium", "Medium"))

        s.check("tr7 load size sensor name",
                client.last_json(
                    "homeassistant/sensor/speedqueen_tr7/load_size/config")["name"],
                "Load Size")
        s.check("tr7 soil sensor name",
                client.last_json(
                    "homeassistant/sensor/speedqueen_tr7/soil/config")["name"],
                "Soil Level")
        s.check("tr7 temp sensor name",
                client.last_json(
                    "homeassistant/sensor/speedqueen_tr7/temp/config")["name"],
                "Water Temp")
        s.check("tr7 state icon",
                client.last_json(
                    "homeassistant/sensor/speedqueen_tr7/state/config")["icon"],
                "mdi:washing-machine")
        lid = client.last_json(
            "homeassistant/binary_sensor/speedqueen_tr7/lid_lock/config")
        # "lock" reads on = Unlocked in Home Assistant, backwards for this LED
        s.check("tr7 lid lock has no device class", "device_class" in lid, False)
        s.check("tr7 lid lock label", lid["name"], "Lid Lock")
        dev = client.last_json(
            "homeassistant/sensor/speedqueen_tr7/display/config")["device"]
        s.check("tr7 device", (dev["identifiers"], dev["model"]),
                (["speedqueen_tr7"], "TR7"))
        # 33 indicators - 22 collapsed = 11 binary, + 4 collapsed, + 4, + camera
        s.check("tr7 entity count", len(client.discovery_topics()), 11 + 4 + 4 + 1)

        # ---- the TR7 panel has no Complete light ------------------------
        s.check("tr7 has no done rule",
                [name for name, _ in decoder.PROFILES["tr7"]["states"]], ["running"])
        client, _ = poll(decoder, tr7, wpos, ["normal_eco"], " ")
        s.check("tr7 selected but idle is ready",
                client.last_json("tr7/panel/state")["state"], "ready")
        client, _ = poll(decoder, tr7, wpos, [], " ")
        s.check("tr7 dark panel is off",
                client.last_json("tr7/panel/state")["state"], "off")
        client, _ = poll(decoder, tr7, wpos, ["lid_lock"], " ")
        s.check("tr7 a lock alone is off",
                client.last_json("tr7/panel/state")["state"], "off")

        # ---- the channel matters: red inverts the TR7 -------------------
        red = synth.calibration("tr7", led_defs(tool, "tr7"), channel="r")
        red_path = os.path.join(tmp, "tr7-on-red.json")
        json.dump(red, open(red_path, "w"))
        wrong = decoder.Machine({"type": "tr7", "snapshot_url": "u",
                                 "calibration_path": red_path})
        client, _ = poll(decoder, wrong, wpos, ["normal_eco", "wash"], "49")
        st = client.last_json("tr7/panel/state")
        s.check("red: the lit status light reads off", st["wash"], "OFF")
        s.check("red: an unlit status light reads on", st["rinse"], "ON")
        s.check("red: the lit cycle is lost", st["cycle"] != "Normal Eco", True)
        s.check("tr7 profile asks for blue", decoder.PROFILES["tr7"]["channel"], "b")
        s.check("dr7 profile still asks for red",
                decoder.PROFILES["dr7"]["channel"], "r")

        # ---- several machines side by side ------------------------------
        specs = [
            {"type": "dr7", "snapshot_url": "http://d/",
             "calibration_path": os.path.join(tmp, "dr7.json")},
            {"type": "tr7", "snapshot_url": "http://w/",
             "calibration_path": os.path.join(tmp, "tr7.json")},
        ]
        pair = decoder.build_machines(json.dumps(specs))
        s.check("two machines keep their default ids", [m.id for m in pair],
                ["dr7", "tr7"])
        s.check("two machines get distinct topics",
                len({m.state_topic for m in pair}), 2)

        custom = decoder.build_machines(json.dumps([{
            "type": "dr7", "id": "Upstairs_DR7", "name": "Upstairs dryer",
            "snapshot_url": "http://d/",
            "calibration_path": os.path.join(tmp, "dr7.json"),
            "poll_active": 5, "poll_idle": 120}]))[0]
        s.check("a custom id is lowercased", custom.id, "upstairs_dr7")
        s.check("a custom id drives the unique_id prefix", custom.uid,
                "speedqueen_upstairs_dr7")
        s.check("a custom name reaches the device", custom.device["name"],
                "Upstairs dryer")
        s.check("custom poll intervals", (custom.poll_active, custom.poll_idle),
                (5, 120))

        # ---- configurations that must be refused ------------------------
        s.expect_error("an unknown machine type",
                       lambda: decoder.build_machines(json.dumps(
                           [{"type": "xr9", "snapshot_url": "u",
                             "calibration_path": "p"}])),
                       "unknown machine type")
        s.expect_error("no machines at all",
                       lambda: decoder.build_machines("[]"), "no machines")
        s.expect_error("two machines sharing an id",
                       lambda: decoder.build_machines(json.dumps([
                           {"type": "dr7", "snapshot_url": "u",
                            "calibration_path": "p"},
                           {"type": "dr7", "snapshot_url": "u2",
                            "calibration_path": "p2"}])),
                       "share the id")
        s.expect_error("an id that is not topic-safe",
                       lambda: decoder.build_machines(json.dumps([
                           {"type": "dr7", "id": "up/stairs", "snapshot_url": "u",
                            "calibration_path": "p"}])),
                       "must be letters")
        s.expect_error("a missing snapshot_url",
                       lambda: decoder.build_machines(json.dumps(
                           [{"type": "dr7", "calibration_path": "p"}])),
                       "no snapshot_url")
        s.expect_error("a missing calibration_path",
                       lambda: decoder.build_machines(json.dumps(
                           [{"type": "dr7", "snapshot_url": "u"}])),
                       "no calibration_path")

        def mismatched():
            decoder.Machine({"type": "tr7", "snapshot_url": "u",
                             "calibration_path": os.path.join(tmp, "dr7.json")}
                            ).ensure_reader()
        s.expect_error("a calibration file for the wrong machine",
                       mismatched, "was built for")

        def collides(name, filename):
            cal = synth.calibration("dr7", led_defs(tool, "dr7"))
            cal["leds"][0]["name"] = name
            p = os.path.join(tmp, filename)
            json.dump(cal, open(p, "w"))
            decoder.Machine({"type": "dr7", "snapshot_url": "u",
                             "calibration_path": p}).ensure_reader()
        s.expect_error("an indicator named like a payload key",
                       lambda: collides("state", "collide-reserved.json"),
                       "reserved")
        s.expect_error("an indicator named like a collapsed group",
                       lambda: collides("cycle", "collide-group.json"),
                       "reserved")

        # ---- one machine's failure must not touch the others ------------
        down, _ = build(decoder, tool, "dr7", tmp)
        down.ensure_reader()

        def unreachable(url, timeout=10):
            raise urllib.error.URLError("no route to host")
        decoder.fetch = unreachable
        client = FakeClient()
        interval = down.poll(client, True)
        s.check("an unreachable camera goes offline",
                ("dr7/panel/availability", "offline", True) in client.pubs, True)
        s.check("an unreachable camera backs off", interval, 60)

        waiting = decoder.Machine({"type": "tr7", "snapshot_url": "u",
                                   "calibration_path": os.path.join(tmp, "absent.json")})
        client = FakeClient()
        s.check("a missing calibration file just waits",
                waiting.poll(client, True), 60)
        s.check("a waiting machine publishes nothing", client.pubs, [])

    return s


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
