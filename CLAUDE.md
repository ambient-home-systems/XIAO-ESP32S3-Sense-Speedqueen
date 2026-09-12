# CLAUDE.md

Context for working on this repository.

## What this is

A Home Assistant add-on repository. A XIAO ESP32S3 Sense photographs a Speed
Queen DR7 dryer's control panel; an add-on decodes the frame into ~20 entities
published over MQTT discovery.

Three components, deliberately separate:

- `esphome/dryer-cam.yaml` — the camera node. Its only job is producing a
  consistent JPEG at a fixed URL.
- `tools/dr7-calibrate.html` — a single-file browser tool that produces
  `calibration.json`. Never uploads anything; runs from `file://`.
- `dr7_panel/` — the add-on. Reads the snapshot, samples ROIs, publishes.

## Hard constraints

- **No machine learning anywhere in the decode path.** Indicators are a median
  over a small disc compared against a per-ROI threshold. Digits are seven
  rectangle medians per character, assembled into a 7-bit pattern and looked up
  in `GLYPHS`. This is a deliberate choice, not a placeholder — it's why
  diagnostic codes (`nH`, `AF`, `dL`) decode at all, and why there are no
  confidence scores to tune. Do not propose a CNN.
- **Calibration data never enters the repo.** `calibration.json` is
  gitignored, as are image files. It's specific to one physical mount.
- **The calibration tool stays a single file with no dependencies.** It runs
  off a USB stick on a laundry-room laptop if it has to.
- **Bump `version` in `dr7_panel/config.yaml` on every pushed change.** It's the
  only thing Supervisor compares when deciding whether an update exists.
- **No GitHub Actions.** Actions minutes are constrained on this account. If CI
  becomes worth it, raise it as a decision rather than adding a workflow.

## Decisions already settled

Don't re-litigate these without new evidence:

- **Sample the red channel, not blue.** The DR7 display has a lit blue
  backlight field behind the digits that is nearly as saturated as the segments
  themselves. Blue thresholding lights up the whole display block. Lit segments
  clip toward white, so red separates them cleanly.
- **Cycle, Temp and Dryness collapse to one sensor each; Status, Options and
  Alerts do not.** Verified from photos: Sensing and Heating are simultaneously
  lit mid-cycle.
- **Anchors are the printed ▲▼ triangles beside the display**, not the panel
  bezel. They're visible in every machine state and sit millimetres from the
  digits, so lens distortion barely affects them. Two points drive a similarity
  transform (translation, rotation, uniform scale).
- **Exposure, gain and white balance are locked in the ESPHome config.** Auto
  exposure hunting between the black panel and bright LEDs blooms segments
  together and makes thresholds drift frame to frame. A longer `aec_value` also
  integrates across display multiplex cycles, preventing half-lit captures.
- **One retained JSON state topic**, with every entity using a
  `value_template` against it. Entities can't disagree with each other and
  there's one publish per poll.
- **`decode_problem` exists because silent failure is the real risk.** A bumped
  camera would otherwise freeze every entity at its last value indefinitely.
- **An unrecognised glyph sets `decode_problem`; a valid fault code does not.**
  A fault code is a correct read of a real condition.

## Not yet verified against hardware

- The camera pin block in `dryer-cam.yaml` is the community mapping for this
  board, not an official ESPHome board profile.
- `aec_value: 300` is a starting guess. Needs tuning against the real panel.
- Newer Sense boards ship an OV3660 rather than the discontinued OV2640.
- The decoder has been exercised against synthetic frames only, including a
  deliberately offset camera to confirm the anchor transform. It has never seen
  a real photograph.

## Conventions

- Python: standard library plus numpy, Pillow, paho-mqtt. Adding a runtime
  dependency means adding an apk or pip line to the Dockerfile, so weigh it.
- Keep `decoder.py` a single module. It's small and the deployment story is
  simpler that way.
- `dr7_panel/DOCS.md` is what Supervisor renders in the add-on's Documentation
  tab. The root `README.md` is for GitHub visitors.

## Unrelated

The `§11` house rules from the `silver-guacamole` / SILO Monitor project do not
apply here.
