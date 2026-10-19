"""0.8.0 checks: placing a moved panel by its reference picture, and the
optional AI reader - parsing a model's answer, both providers' requests, and
how Machine.fuse() settles a disagreement with the decoder.

No network: the providers' HTTP calls and the camera are swapped for fakes.
"""

import io
import json
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

RUNNING = ["regular", "medium", "dry", "sensing", "heating"]


class FakeAI:
    """Answers from a script instead of a model."""

    def __init__(self, answers, escalate=None, every=60):
        self.answers = list(answers)
        self.escalations = list(escalate or [])
        self.every = every
        self.enabled = True
        self.calls = []
        self.stats = {"calls": 0}

    @property
    def can_escalate(self):
        return bool(self.escalations)

    def describe(self, escalate=False):
        return "second" if escalate else "first"

    def read(self, mid, profile, names, jpeg, escalate=False):
        self.calls.append("second" if escalate else "first")
        self.stats["calls"] += 1
        pool = self.escalations if escalate else self.answers
        display, lit = pool.pop(0)
        return {"display": display, "lit": list(lit), "model": self.describe(escalate),
                "secs": 0.0, "input_tokens": None, "output_tokens": None}


def garbled(text, positions):
    """A DR7 frame whose first digit shows a pattern that is no glyph: the
    text drawn, plus segment a lit on digit 1 (a "4" with a top bar)."""
    frame = synth.render(set(RUNNING), text, positions, machine="dr7")
    img = Image.open(io.BytesIO(frame)).convert("RGB")
    x, y, w, h = synth.seg_rects(synth.DIGIT_BOXES[0])["a"]
    ImageDraw.Draw(img).rectangle([x, y, x + w, y + h], fill=synth.PALETTE["dr7"]["seg_lit"])
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def run(s=None):
    s = s or Suite("ai")
    import decoder
    real_fetch, real_post = decoder.fetch, decoder.post_json
    decoder.CONFIRM_GAP = decoder.FETCH_RETRY_DELAY = 0
    tool = toollists.machines()

    # --- a model's answer, normalised --------------------------------------
    nd = decoder.normalize_display
    for raw, want in (("45", "45"), ("07", "07"), ("7", " 7"), ("", "  "), ("  ", "  "),
                      ('"38"', "38"), ("nH", "nH"), ("NH", "nH"), ("O5", "05"),
                      ("4?", "4?"), ("12345", "??"), (" 9 ", " 9"), (None, "  "), ("-n", "-n")):
        s.check(f"display {raw!r} normalised", nd(raw), want)
    names = ["heavy_duty", "lid_lock", "temp_warm_cold", "wash"]
    s.check("lit names from a comma list", decoder.normalize_lit("Heavy Duty, lid-lock, nonsense", names),
            {"heavy_duty", "lid_lock"})
    s.check("lit names from a JSON list", decoder.normalize_lit(["wash", "TEMP_WARM_COLD"], names),
            {"wash", "temp_warm_cold"})
    s.check("a JSON object inside prose", decoder.parse_json_object('```json\n{"display": "45", "lit": []}\n```'),
            {"display": "45", "lit": []})
    s.check("no JSON object", decoder.parse_json_object("I cannot read this panel"), None)

    # --- configuration -----------------------------------------------------
    s.check("no ai option = off", decoder.AIReader({}).enabled, False)
    s.expect_error("home_assistant needs an AI Task entity",
                   lambda: decoder.AIReader({"provider": "home_assistant"}), "entity_id")
    s.expect_error("an entity that is not an AI Task",
                   lambda: decoder.AIReader({"provider": "home_assistant", "entity_id": "camera.x"}),
                   "AI Task")
    s.expect_error("openai_compatible needs base_url and model",
                   lambda: decoder.AIReader({"provider": "openai_compatible", "base_url": "http://x"}),
                   "base_url and model")

    profile = decoder.PROFILES["tr7"]
    with tempfile.TemporaryDirectory() as tmp:
        # --- the OpenAI-compatible request and answer -------------------------
        sent = {}

        def fake_post(url, body, headers=None, timeout=None):
            sent.update(url=url, body=body, headers=headers or {})
            return {"choices": [{"message": {"content": 'Sure: {"display": "47", "lit": ["wash", "lid_lock"]}'}}],
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 30}}

        decoder.post_json = fake_post
        oa = decoder.AIReader({"provider": "openai_compatible", "base_url": "https://llm.example/v1/",
                               "api_key": "k", "model": "qwen-vl-plus", "max_calls_per_day": 2})
        got = oa.read("tr7", profile, names, b"\xff\xd8jpeg")
        s.check("openai: answer read", (got or {}).get("display"), "47")
        s.check("openai: lit read", (got or {}).get("lit"), ["lid_lock", "wash"])
        s.check("openai: endpoint", sent.get("url"), "https://llm.example/v1/chat/completions")
        s.check("openai: model", sent["body"]["model"], "qwen-vl-plus")
        s.check("openai: key sent as a bearer token", sent["headers"].get("Authorization"), "Bearer k")
        s.truthy("openai: frame sent as a data URL",
                 sent["body"]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        s.check("openai: tokens counted", oa.stats["input_tokens"], 1200)
        oa.read("tr7", profile, names, b"x")
        s.check("the daily budget stops the third read", oa.read("tr7", profile, names, b"x"), None)

        # --- the Home Assistant AI Task request -------------------------------
        def ha_post(url, body, headers=None, timeout=None):
            sent.update(url=url, body=body)
            return {"service_response": {"conversation_id": "c", "data": {"display": "05", "lit": "wash"}}}

        decoder.post_json = ha_post
        media = os.path.join(tmp, "media")
        decoder.AI_MEDIA_ROOT = tmp
        ha = decoder.AIReader({"provider": "home_assistant", "entity_id": "ai_task.panel",
                               "escalate_entity_id": "ai_task.bigger", "media_dir": os.path.join(media, "sq")})
        got = ha.read("dr7", decoder.PROFILES["dr7"], ["wash"], b"\xff\xd8frame")
        s.check("ha: answer read", (got or {}).get("display"), "05")
        s.truthy("ha: generate_data with a response", sent["url"].endswith("/services/ai_task/generate_data?return_response"))
        s.check("ha: entity", sent["body"]["entity_id"], "ai_task.panel")
        s.check("ha: frame attached from the media source",
                sent["body"]["attachments"][0]["media_content_id"], "media-source://media_source/local/media/sq/dr7.jpg")
        s.check("ha: frame written there", open(os.path.join(media, "sq", "dr7.jpg"), "rb").read(), b"\xff\xd8frame")
        ha.read("dr7", decoder.PROFILES["dr7"], ["wash"], b"f", escalate=True)
        s.check("ha: a tie goes to the second entity", sent["body"]["entity_id"], "ai_task.bigger")

        def failing(url, body, headers=None, timeout=None):
            raise OSError("connection refused")

        decoder.post_json = failing
        s.check("a failed call is no answer", ha.read("dr7", decoder.PROFILES["dr7"], [], b"f"), None)
        s.truthy("...and is recorded", "refused" in (ha.stats["last_error"] or ""))
        decoder.post_json = real_post

        # --- registration: the panel placed by its reference picture ---------
        defs = [(l["group"], l["name"], l["label"], synth.led_grid(len(tool["dr7"]["leds"]))[i])
                for i, l in enumerate(tool["dr7"]["leds"])]
        pos = {n: xy for _, n, _, xy in defs}
        still = Image.open(io.BytesIO(synth.render(set(RUNNING), "46", pos))).convert("RGB")
        shifted = Image.open(io.BytesIO(synth.render(set(RUNNING), "46", pos,
                                                     warp=synth.Warp(dx=-37, dy=21)))).convert("RGB")
        got = decoder.register_frames(still, shifted)
        s.check("a moved frame is found to the pixel", got[:2], (-37, 21))
        s.truthy("...with a clear lead", got[3] >= decoder.REG_MIN_LEAD)
        s.check("an unmoved frame is found in place", decoder.register_frames(still, still)[:2], (0, 0))
        dark = Image.open(io.BytesIO(synth.render(set(), "  ", pos, warp=synth.Warp(dx=25, dy=-12)))).convert("RGB")
        s.check("a dark panel is placed too (only the printed panel lit)",
                decoder.register_frames(still, dark)[:2], (25, -12))

        bare = synth.calibration("dr7", defs)
        bare["anchors"] = []
        path = os.path.join(tmp, "dr7.json")
        json.dump(bare, open(path, "w"))

        def poll(m, frame):
            decoder.fetch = lambda url, timeout=10: frame
            client = FakeClient()
            m.poll(client, False)
            return client.last_json(f"{m.id}/panel/state")

        reg = decoder.Machine({"type": "dr7", "snapshot_url": "u", "calibration_path": path})
        reg.ensure_reader()
        buf = io.BytesIO()
        still.save(buf, format="JPEG", quality=92)
        reg.save_reference(buf.getvalue(), (0, 0), "46")
        s.truthy("reference saved beside the calibration", os.path.exists(os.path.join(tmp, "dr7.reference.jpg")))
        st = poll(reg, synth.render(set(RUNNING), "33", pos, warp=synth.Warp(dx=-45, dy=25)))
        s.check("a moved camera reads after registering", st["display"], "33")
        s.check("...placed exactly", reg.reader.offset, (-45, 25))
        s.check("...state", st["state"], "running")
        reg2 = decoder.Machine({"type": "dr7", "snapshot_url": "u", "calibration_path": path})
        st = poll(reg2, synth.render(set(RUNNING), "21", pos, warp=synth.Warp(dx=-45, dy=25)))
        s.check("a restart finds the panel from the saved reference", st["display"], "21")

        # --- fuse(): the AI reader beside the decoder -----------------------
        def machine(ai):
            m = decoder.Machine({"type": "dr7", "snapshot_url": "u", "calibration_path": path})
            m.ai = ai
            return m

        frame46 = synth.render(set(RUNNING), "46", pos)
        both = machine(FakeAI([("46", RUNNING)]))
        st = poll(both, frame46)
        s.check("agreement: display", st["display"], "46")
        s.check("agreement: read by both", st["read_by"], "both")
        s.check("agreement: AI display published", st["ai_display"], "46")

        rescue = machine(FakeAI([("46", RUNNING)]))
        st = poll(rescue, garbled("46", pos))
        s.check("a garbled decoder read: the model's display", st["display"], "46")
        s.check("...read by the AI", st["read_by"], "ai")
        s.check("...not a decode problem", st["decode_problem"], "OFF")
        s.check("...reader ok", st["reader"], "ok")
        s.check("...the model's lights used", st["state"], "running")

        tie = machine(FakeAI([("45", RUNNING)], escalate=[("46", RUNNING)]))
        st = poll(tie, frame46)
        s.check("a tie goes to the majority with the second model", st["display"], "46")
        s.check("...second model asked", tie.ai.calls, ["first", "second"])

        overruled = machine(FakeAI([("45", RUNNING)], escalate=[("45", RUNNING)]))
        st = poll(overruled, frame46)
        s.check("two models against the decoder win", st["display"], "45")
        s.check("...read by the AI", st["read_by"], "ai")

        # the countdown decides when there is no second model
        counted = machine(FakeAI([("46", RUNNING), ("45", RUNNING), ("98", RUNNING)]))
        for shown in ("46", "45"):                   # the decoder and the model count down together
            counted.ai_last = float("-inf")
            poll(counted, synth.render(set(RUNNING), shown, pos))
        counted.ai_last = float("-inf")
        st = poll(counted, synth.render(set(RUNNING), "44", pos))
        s.check("a model misread the countdown rules out is ignored", st["display"], "44")
        s.check("...read by the panel", st["read_by"], "panel")
        s.check("...the model's answer still published", st["ai_display"], "98")

        # with no countdown and no second model, the model's answer stands
        first = machine(FakeAI([("45", RUNNING)]))
        st = poll(first, frame46)
        s.check("no countdown, no second model: the model's answer", st["display"], "45")

        quiet = machine(FakeAI([("46", RUNNING)], every=60))
        poll(quiet, frame46)
        st = poll(quiet, frame46)
        s.check("no second model call within `every`", quiet.ai.calls, ["first"])
        s.check("...still read by both while the decoder agrees", st["read_by"], "both")
        st = poll(quiet, garbled("46", pos))
        s.check("...but not once the decoder is unsure", st["read_by"], "panel")
        idle = machine(FakeAI([("46", RUNNING)], every=60))
        poll(idle, frame46)
        st = poll(idle, synth.render(set(), "  ", pos))
        s.check("...nor once the machine is idle", st["read_by"], "panel")

    decoder.fetch, decoder.post_json = real_fetch, real_post
    return s
