# CLAUDE.md

Context for working on this repository.

## What this is

A Home Assistant add-on repository. A XIAO ESP32S3 Sense photographs a Speed
Queen 7-series control panel; an add-on decodes the frame into ~20 entities
published over MQTT discovery. Two machines are supported: the DR7 dryer and
the TR7 washer.

Three components, deliberately separate:

- `esphome/panel-cam-base.yaml` — the camera block. One thin file per node
  (`dryer-cam.yaml`, `washer-cam.yaml`) pulls it from this repository over
  `github://…@main`, so a user copies one file rather than two. Its only job
  is producing a consistent JPEG at a fixed URL.
- `tools/sq-calibrate.html` — a single-file browser tool that produces
  `calibration.json`. Never uploads anything; runs from `file://`.
- `speedqueen_panel/` — the add-on. Reads the snapshots, samples ROIs,
  publishes. One instance drives every machine.

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
- **Bump `version` in `speedqueen_panel/config.yaml` on every pushed change.**
  It's the only thing Supervisor compares when deciding whether an update
  exists.
- **No GitHub Actions.** Actions minutes are constrained on this account. If CI
  becomes worth it, raise it as a decision rather than adding a workflow.

## Decisions already settled

Don't re-litigate these without new evidence:

- **The sampling channel is per machine — red for the DR7, blue for the TR7.**
  Both were measured, and they genuinely disagree:
  - *DR7:* the display has a lit blue backlight field behind the digits, nearly
    as saturated as the segments themselves, so blue thresholding lights up the
    whole display block. Lit segments clip toward white, so red separates them.
  - *TR7:* lit indicators are saturated blue (R≈60 G≈57 B≈243) while the unlit
    dots are neutral grey (≈133 in every channel). In red — and in luma — a lit
    LED therefore reads *darker* than an unlit one and every indicator decodes
    backwards. Blue splits them by ≈70 levels; its display segments clear the
    backlight field by ≈80 levels in blue against ≈12 in red.

  The channel lives in `calibration.json`, so this is a per-file setting the
  tool picks by machine. `PROFILES[...]["channel"]` is what the decoder expects
  and it warns on a mismatch rather than failing — a panel in different light
  might genuinely differ.
- **Collapsed groups are per machine; Status, Options and Alerts never
  collapse.** Verified from photos: on the DR7, Sensing and Heating are
  simultaneously lit mid-cycle. DR7 collapses Cycle, Temp and Dryness; TR7
  collapses Cycle, Water Temp, Load Size and Soil Level.
- **The TR7 has no Complete light**, so its `state` never reports `done` — the
  end of a cycle is only visible as `running` → `ready`. Don't invent a `done`
  rule for it without knowing what the panel actually does at the end.
- **Indicator names may differ from the printed legend to avoid collisions.**
  Names share one flat payload namespace, so the TR7's "Spin" wash cycle is
  `spin_cycle` (Status "Spin" owns `spin`) and its two "Medium" rows are
  prefixed `load_` and `soil_`. Labels are what reach Home Assistant, and those
  always match the panel.
- **Anchors are the printed ▲▼ triangles beside the display**, not the panel
  bezel. They're visible in every machine state and sit millimetres from the
  digits, so lens distortion barely affects them. Two points drive a similarity
  transform (translation, rotation, uniform scale).
- **Node files carry the Wi-Fi credentials; the shared base carries only the
  fallback hotspot.** Credentials must stay out of `panel-cam-base.yaml`: a
  node that got its credentials from the compiled config has nothing saved in
  flash, so removing them from under an already-flashed node would strand it
  on the next OTA. `captive_portal` plus the `ap:` fallback means a node that
  cannot join the network asks for a new one instead of needing a USB cable,
  and a node with no `wifi:` block at all can be provisioned that way from new.
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
- **A machine's MQTT identity is its `id`, which defaults to its type.** So a
  lone DR7 still publishes to `dr7/panel/…` with `speedqueen_dr7_…` unique IDs,
  exactly as 0.1.x did. This is what let the add-on be renamed without
  orphaning anyone's entities, and it's why `id` must never be changed once a
  machine is running. The add-on name and slug are free to change; these
  strings are not.
- **Machine differences are data, not code paths.** `PROFILES` in `decoder.py`
  holds each machine's groups, labels, state rules and icons; the sampling,
  transform and glyph machinery is shared because every 7-series control uses
  the same display and the same printed triangles. A new machine should be a
  new profile plus a new list in the calibration tool, nothing more.
- **The calibration tool owns the indicator legend.** It defines the click
  order and ships each indicator's label in `calibration.json`; the add-on
  prefers those labels over its own. Correcting a misread legend shouldn't
  need a Python change.
- **One add-on instance, many machines.** Supervisor can't install an add-on
  twice, so the machine list is a config array. Per-machine failures are
  isolated: one unreachable camera or missing calibration file must never stop
  the others publishing.

## Not yet verified against hardware

- The camera pin block in `panel-cam-base.yaml` is the community mapping for
  this board, not an official ESPHome board profile.
- The ESPHome configs have been checked against ESPHome's own package and
  substitution passes, never compiled or flashed from here. The `github://`
  package in particular has only been verified as far as the shorthand
  resolving to the right raw URL — the fetch itself needs the file to exist on
  `main`.
- `aec_value: 300` is a starting guess. Needs tuning against the real panel.
- Newer Sense boards ship an OV3660 rather than the discontinued OV2640.
- The decoder has been exercised against synthetic frames only, including a
  deliberately offset camera to confirm the anchor transform. It has never seen
  a real photograph.
- **The TR7's levels under the mounted camera.** The channel choice and the
  lit/unlit levels above were measured off a phone photograph in room light,
  not through the XIAO with locked exposure and a hood. The *inversion* is
  structural and will hold; the absolute numbers will move, which is what
  calibration is for.
- **What the TR7 shows at the end of a cycle.** There is no Complete light. If
  the display or the Signal LED marks the end, a `done` rule becomes possible.
- The TR7's ▲▼ sit to the right of its digits and look printed, like the DR7's,
  but that hasn't been confirmed across machine states.

## Conventions

- Python: standard library plus numpy, Pillow, paho-mqtt. Adding a runtime
  dependency means adding an apk or pip line to the Dockerfile, so weigh it.
- Keep `decoder.py` a single module. It's small and the deployment story is
  simpler that way.
- `speedqueen_panel/DOCS.md` is what Supervisor renders in the add-on's
  Documentation tab. The root `README.md` is for GitHub visitors.
- Indicator names are flat keys in one JSON payload, so they can't collide with
  each other, with an exclusive group name, or with `display`,
  `time_remaining`, `state`, `active`, `raw` or `decode_problem`. `Machine.validate`
  rejects that at startup rather than letting a key be silently overwritten.
- **Run `python3 tests/run.py` before pushing a change to `decoder.py` or to
  the tool's `MACHINES` table.** Nothing runs it for you — there's no CI here
  by design. It checks the tool and `PROFILES` against each other (they drift
  silently otherwise) and decodes synthetic frames, pinning the DR7's MQTT
  identity strings and the per-machine sampling channel in particular. See
  `tests/README.md`, including what to add when a new machine appears.
- `tests/` is outside the add-on's build context, so nothing there ships in the
  image and adding a check never needs a `version` bump.

## Unrelated

The `§11` house rules from the `silver-guacamole` / SILO Monitor project do not
apply here.
