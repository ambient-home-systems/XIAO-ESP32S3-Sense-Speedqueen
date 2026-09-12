"""The ingress UI: serving the tool, proxying snapshots, saving calibrations.

The save endpoint writes into the Home Assistant config directory, so most of
what is checked here is what it refuses. The destination always comes from the
add-on's own configuration, never from the request, which is what keeps a
request from writing anywhere the add-on was not already pointed at.
"""

import hashlib
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

            s.check("no refused save changed the file", digest(), before)
            s.check("no stray temp file was left",
                    os.path.exists(written + ".tmp"), False)

            # a calibration with no machine field still saves, since older
            # exports predate it
            legacy = dict(good)
            legacy.pop("machine")
            code, _ = post(base + "/api/calibration/dr7", legacy)
            s.check("a calibration with no machine field is accepted", code, 200)

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
