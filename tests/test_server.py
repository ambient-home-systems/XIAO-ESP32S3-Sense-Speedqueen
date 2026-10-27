"""The ingress UI: serving the tool, proxying snapshots, saving calibrations.

The save endpoint writes into the Home Assistant config directory, so most of
what is checked here is what it refuses. The destination always comes from the
add-on's own configuration, never from the request, which is what keeps a
request from writing anywhere the add-on was not already pointed at.
"""

import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "speedqueen_panel"))
sys.path.insert(0, str(ROOT / "tests"))

import synth                                        # noqa: E402
from PIL import Image                               # noqa: E402
import toollists                                    # noqa: E402
from harness import Suite                           # noqa: E402


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as res:
            return res.status, res.read(), res.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def post(url, body):
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    req = urllib.request.Request(url, data=body.encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def run(s=None):
    """Accepts a Suite so a caller keeps partial results if a check raises."""
    import decoder

    s = s or Suite("server")
    tool = toollists.machines()

    with tempfile.TemporaryDirectory() as tmp:
        cal_dir = os.path.join(tmp, "nested", "config")      # must be created
        specs = [
            {"type": "dr7", "snapshot_url": "http://dryer.invalid/",
             "calibration_path": os.path.join(cal_dir, "dr7.json")},
            {"type": "tr7", "snapshot_url": "http://washer.invalid/",
             "calibration_path": os.path.join(cal_dir, "tr7.json")},
        ]
        machines = decoder.build_machines(json.dumps(specs))

        httpd = decoder.serve_calibration_ui(
            machines, port=0, tool_path=str(ROOT / "speedqueen_panel" / "sq-calibrate.html"))
        if httpd is None:
            s.failures.append("the UI server did not start")
            return s
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        s.note(f"serving on {base}")

        try:
            # ---- the page itself ---------------------------------------
            code, body, ctype = get(base + "/")
            s.check("GET / serves the tool", code, 200)
            s.check("GET / is HTML", ctype.startswith("text/html"), True)
            s.check("GET / is the calibration page",
                    b"Speed Queen panel calibration" in body, True)
            s.check("the page ships both machine lists",
                    (b"dr7:" in body and b"tr7:" in body), True)

            # ---- the machine list --------------------------------------
            code, body, ctype = get(base + "/api/machines")
            s.check("GET api/machines", code, 200)
            s.check("api/machines is JSON", ctype.startswith("application/json"), True)
            listed = json.loads(body)
            s.check("both machines listed", [m["id"] for m in listed], ["dr7", "tr7"])
            s.check("each carries its type", [m["type"] for m in listed], ["dr7", "tr7"])
            s.check("each carries its channel",
                    [m["channel"] for m in listed], ["r", "b"])
            s.check("nothing calibrated yet",
                    [m["has_calibration"] for m in listed], [False, False])
            s.check("the destination path is disclosed",
                    listed[0]["calibration_path"], specs[0]["calibration_path"])

            # ---- the snapshot proxy ------------------------------------
            defs = [(l["group"], l["name"], l["label"], xy) for l, xy in zip(
                tool["dr7"]["leds"], synth.led_grid(len(tool["dr7"]["leds"])))]
            frame = synth.render({"regular"}, "46",
                                 {n: xy for _, n, _, xy in defs}, machine="dr7")
            decoder.fetch = lambda url, timeout=10: frame
            code, body, ctype = get(base + "/api/snapshot/dr7")
            s.check("GET api/snapshot/<id>", code, 200)
            s.check("the proxy serves JPEG", ctype, "image/jpeg")
            s.check("the proxy returns the camera's bytes", body, frame)

            code, _, _ = get(base + "/api/snapshot/nope")
            s.check("an unknown machine is not found", code, 404)
            # an id shaped like a traversal matches no configured machine
            code, _, _ = get(base + "/api/snapshot/..%2F..%2Fetc")
            s.check("a traversal-shaped id is not found", code, 404)

            def unreachable(url, timeout=10):
                raise urllib.error.URLError("no route to host")
            decoder.fetch = unreachable
            code, body, _ = get(base + "/api/snapshot/dr7")
            s.check("an unreachable camera is a bad gateway", code, 502)
            s.check("and says so", b"camera unreachable" in body, True)
            decoder.fetch = lambda url, timeout=10: frame

            # ---- reading the saved calibration back (0.8.4) -------------
            code, body, _ = get(base + "/api/calibration/dr7")
            s.check("nothing saved yet is not found", (code, b"nothing calibrated" in body), (404, True))
            code, _, _ = get(base + "/api/calibration/nope")
            s.check("an unknown machine's calibration is not found", code, 404)

            # ---- saving a calibration ----------------------------------
            good = synth.calibration("dr7", defs)
            code, body = post(base + "/api/calibration/dr7", good)
            s.check("a valid calibration is accepted", code, 200)
            written = specs[0]["calibration_path"]
            s.check("it is written where the config said", os.path.exists(written), True)
            s.check("the response names the path", json.loads(body)["saved"], written)
            saved = json.load(open(written))
            s.check("what was written is what was sent",
                    (saved["machine"], len(saved["leds"])), ("dr7", len(good["leds"])))
            code, body, ctype = get(base + "/api/calibration/dr7")
            s.check("the saved calibration reads back for editing",
                    (code, ctype, json.loads(body)), (200, "application/json", saved))
            s.check("the machine will reload it", machines[0].reader, None)
            s.check("and re-announce discovery", machines[0].announced, False)

            code, body, _ = get(base + "/api/machines")
            s.check("the listing now shows it calibrated",
                    json.loads(body)[0]["has_calibration"], True)

            # ---- saves that must be refused ----------------------------
            def digest():
                return hashlib.sha256(open(written, "rb").read()).hexdigest()[:16]
            before = digest()

            code, body = post(base + "/api/calibration/tr7", good)
            s.check("a DR7 calibration is refused for the TR7", code, 400)
            s.check("and explains why", b"built for" in body, True)
            s.check("the TR7 file was not created",
                    os.path.exists(specs[1]["calibration_path"]), False)

            code, body = post(base + "/api/calibration/dr7", "{not json")
            s.check("malformed JSON is refused", code, 400)
            s.check("and says so", b"not valid JSON" in body, True)

            code, body = post(base + "/api/calibration/dr7",
                              dict(good, version=99))
            s.check("an unknown version is refused", code, 400)
            s.check("even when the rest of it is valid",
                    b"unsupported calibration version" in body, True)
            code, _ = post(base + "/api/calibration/dr7",
                           {"version": 1, "leds": [], "digits": []})
            s.check("an empty calibration is refused", code, 400)
            code, _ = post(base + "/api/calibration/dr7", [1, 2, 3])
            s.check("a JSON array is refused", code, 400)
            code, _ = post(base + "/api/calibration/nope", good)
            s.check("an unknown machine is not found", code, 404)
            code, _ = post(base + "/api/calibration/..%2F..%2Fescape", good)
            s.check("a traversal-shaped id is not found", code, 404)

            code, body = post(base + "/api/calibration/dr7",
                              dict(good, warp={"src": [[0, 0], [10, 0], [10, 10]], "size": [100, 50]}))
            s.check("a warp without four corners is refused", code, 400)
            s.check("and says so", b"warp" in body, True)
            code, _ = post(base + "/api/calibration/dr7",
                           dict(good, warp={"src": [[0, 0], [10, 0], [20, 0], [30, 0]], "size": [100, 50]}))
            s.check("a warp with its corners in a line is refused", code, 400)

            s.check("no refused save changed the file", digest(), before)
            s.check("no stray temp file was left",
                    os.path.exists(written + ".tmp"), False)

            # a calibration with no machine field still saves, since older
            # exports predate it
            legacy = dict(good)
            legacy.pop("machine")
            code, _ = post(base + "/api/calibration/dr7", legacy)
            s.check("a calibration with no machine field is accepted", code, 200)

            # ---- saved frames, checks and fits (0.7.0) --------------------
            # A labeled frame in the machine's frames folder, as the reader
            # would have saved it.
            dr7 = machines[0]
            dr7.ensure_reader()
            png = synth.render({"regular", "medium", "dry"}, "46",
                               {n: xy for _, n, _, xy in defs}, machine="dr7")
            decoder.fetch = lambda url, timeout=10: png
            dr7.reader.read()
            dr7.frames.save(dr7.reader.chosen, "sample", label="46", source="test")

            code, body, ctype = get(base + "/api/frames/dr7")
            s.check("GET api/frames/<id>", code, 200)
            listed = json.loads(body)
            s.check("it counts the saved frames", (listed["frames"], listed["labeled"]), (1, 1))
            s.check("and names their folder, beside the calibration",
                    listed["folder"], os.path.join(cal_dir, "frames", "dr7"))

            code, body, _ = get(base + "/api/check/dr7")
            s.check("GET api/check/<id> checks the file on disk", code, 200)
            s.check("and reads the saved frame", json.loads(body)["read"], 1)

            before = digest()
            code, body = post(base + "/api/check/dr7", good)
            s.check("POST api/check/<id> checks a calibration it is sent", code, 200)
            s.check("without saving it", digest(), before)
            code, body = post(base + "/api/check/tr7", good)
            s.check("a DR7 calibration is not checked as a TR7's", code, 400)
            code, body = post(base + "/api/check/dr7", "{not json")
            s.check("a malformed one is refused", code, 400)

            code, body, _ = get(base + "/api/fit/dr7")
            s.check("GET api/fit/<id> proposes a fit", code, 200)
            fit = json.loads(body)
            s.check("with its moves, the checks either side, and the calibration",
                    sorted(fit), ["after", "before", "calibration", "moves"])
            s.check("and saves nothing", digest(), before)
            code, _, _ = get(base + "/api/fit/nope")
            s.check("an unknown machine has nothing to fit", code, 404)

            # ---- 0.8.0: the reference picture, registration, AI reader --
            code, body, _ = get(base + "/api/ai/status")
            s.check("GET api/ai/status with the reader off",
                    (code, json.loads(body)), (200, {"provider": "none"}))
            code, _, _ = get(base + "/api/ai/read/dr7")
            s.check("an AI read with the reader off is refused", code, 409)
            code, _, _ = get(base + "/api/ai/bench/dr7")
            s.check("an AI bench with the reader off is refused", code, 409)
            dryer = next(m for m in machines if m.id == "dr7")
            dryer.ensure_reader()
            code, _, _ = get(base + "/api/register/dr7")
            s.check("registering with no reference yet is refused", code, 409)
            still = synth.render(set(), "  ", {})
            decoder.fetch = lambda url, timeout=10: still
            code, body = post(base + "/api/reference/dr7", "")
            s.check("POST api/reference/<id> saves the current frame", code, 200)
            s.truthy("...beside the calibration",
                     os.path.exists(os.path.join(cal_dir, "dr7.reference.jpg")))
            code, body, _ = get(base + "/api/register/dr7")
            s.check("GET api/register/<id> places the panel", code, 200)
            s.check("...unmoved", json.loads(body)["offset"], [0, 0])
            grey = io.BytesIO()
            Image.new("RGB", (synth.W, synth.H), (90, 90, 90)).save(grey, format="JPEG")
            decoder.fetch = lambda url, timeout=10: grey.getvalue()
            code, _ = post(base + "/api/reference/dr7", "")
            s.check("a blocked view is refused as the reference", code, 409)
            code, _ = post(base + "/api/reference/nope", "")
            s.check("no reference for an unknown machine", code, 404)

            # ---- rebase to the camera (0.8.8) ---------------------------
            # The reader places the panel `offset` px from the file; a rebase moves
            # the file's corners there and the reference picture's offset back.
            dr7 = machines[0]
            dr7.ensure_reader()
            with open(dr7.cal_path) as fh:
                cal_before = json.load(fh)
            code, body = post(base + "/api/rebase/dr7", "")
            s.check("a rebase with the panel where the file says changes nothing",
                    (code, json.loads(body)["rebased"]), (200, False))
            os.makedirs(os.path.dirname(dr7.ref_path), exist_ok=True)
            with open(dr7.ref_path + ".json", "w") as fh:
                json.dump({"offset": [-30, 8], "display": "46", "time": "test"}, fh)
            size = tuple(dr7.reader.cal.size) if dr7.reader.cal.size and dr7.reader.cal.size[0] else (640, 480)
            dr7.reader.set_pose(decoder.pose_shift(50, -2), size)
            code, body, _ = get(base + "/api/machines")
            s.check("api/machines reports the reader's placement",
                    json.loads(body)[0]["offset"], [50, -2])
            code, body = post(base + "/api/rebase/dr7", "")
            got = json.loads(body)
            s.check("POST api/rebase/<id> rebases", (code, got["rebased"], got["by"]), (200, True, [50, -2]))
            with open(dr7.cal_path) as fh:
                cal_after = json.load(fh)
            if cal_before.get("warp"):
                want = [[round(x + 50, 1), round(y - 2, 1)] for x, y in cal_before["warp"]["src"]]
            else:
                w, h = cal_before["image_size"]
                want = [[50, -2], [w + 50, -2], [w + 50, h - 2], [50, h - 2]]
            s.check("the four corners moved by the offset", cal_after["warp"]["src"], want)
            s.check("everything else in the file is unchanged",
                    {k: v for k, v in cal_after.items() if k != "warp"},
                    {k: v for k, v in cal_before.items() if k != "warp"})
            with open(dr7.ref_path + ".json") as fh:
                s.check("the reference offset moved back by the same amount",
                        json.load(fh)["offset"], [-80, 10])
            with open(got["backup"]) as fh:
                s.check("the previous file was kept", json.load(fh), cal_before)
            code, _ = post(base + "/api/rebase/nope", "")
            s.check("a rebase of an unknown machine is not found", code, 404)

            # ---- unknown routes ----------------------------------------
            code, _, _ = get(base + "/api/nonsense")
            s.check("an unknown GET route is not found", code, 404)
            code, _ = post(base + "/api/nonsense", good)
            s.check("an unknown POST route is not found", code, 404)

            # ---- a missing tool file is reported, not a crash ----------
            gone = decoder.serve_calibration_ui(
                machines, port=0, tool_path=os.path.join(tmp, "absent.html"))
            if gone is not None:
                code, body, _ = get(f"http://127.0.0.1:{gone.server_address[1]}/")
                s.check("a missing tool file is a server error", code, 500)
                s.check("and names the problem",
                        b"calibration tool missing" in body, True)
                gone.shutdown()
        finally:
            httpd.shutdown()

    return s


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
