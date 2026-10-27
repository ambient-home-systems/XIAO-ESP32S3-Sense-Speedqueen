# Speed Queen panel reader

Reads a Speed Queen 7-series control panel with a camera and publishes about
twenty entities per machine to Home Assistant. The decoder uses no machine
learning: indicators are brightness thresholds, digits are seven-segment
lookups. An optional [AI reader](#ai-reader-optional) can read the same frame
through a vision model and settle disagreements.

One add-on instance handles any number of machines. Each needs its own camera
node and its own calibration file.

## Machines

| `type` | Machine | Indicators | Collapsed groups | Channel |
|---|---|---|---|---|
| `dr7` | DR7 dryer | 31 | Cycle, Temp, Dryness | red |
| `tr7` | TR7 washer | 33 | Cycle, Water Temp, Load Size, Soil Level | blue |

Both legends were read from photographs of the real panels. If yours differs —
a different cycle set, an indicator in a different place — fix it in the
`MACHINES` table at the top of `sq-calibrate.html`. The add-on takes
indicator labels from the calibration file you export, so a renamed legend
needs no add-on change. Adding or removing an indicator, or moving one between
groups, also needs the matching entry in `PROFILES` in `decoder.py`.

### The channel differs between the two machines

The tool picks it for you when you choose the machine, and the add-on warns if
a calibration file disagrees with its machine's profile. The reason they differ
is worth knowing if you ever tune it by hand:

- **DR7 — red.** The digits sit on a lit blue backlight field almost as
  saturated as the segments, so blue lights up the whole display block. Lit
  segments clip toward white, so red separates them.
- **TR7 — blue.** Its lit indicators are saturated blue (R≈60 G≈57 B≈243) while
  the unlit dots are neutral grey (≈133 everywhere). In red, and in luma, a lit
  LED reads *darker* than an unlit one, so every indicator decodes backwards.
  Blue splits them by ≈70 levels.

### The TR7 has no Complete light

Its Status row is Wash / Rinse / Spin only, so `state` goes `running` →
`ready` at the end of a cycle and never reports `done`. To be notified when the
washer finishes, trigger on that transition rather than waiting for `done`:

```yaml
trigger:
  - platform: state
    entity_id: sensor.speed_queen_tr7_state
    from: "running"
    to: "ready"
```

## 1. Flash the cameras

All of this happens in the ESPHome Device Builder add-on — no file manager,
no terminal.

1. **+ New device**, named `washer-cam` (or `dryer-cam`), board **ESP32-S3**,
   and **Skip** the install it offers. The name becomes the hostname, which is
   what makes `http://washer-cam.local:8081/` work later.
2. ESPHome generates an **API encryption key** in the file it just wrote.
   Copy that value into the **Secrets** editor (three-dot menu, top right) as
   `washer_cam_api_key`, and add a `washer_cam_ota_password` of your choosing.
   `wifi_ssid` and `wifi_password` are usually already there from an earlier
   device.
3. **Edit** the device, delete everything in the editor, and paste in this
   repository's `esphome/washer-cam.yaml`. It names the node, points at those
   two secrets, and pulls the camera block from the repository:

   ```yaml
   packages:
     cam:
       url: https://github.com/ambient-home-systems/XIAO-ESP32S3-Sense-Speedqueen
       ref: main
       files: [esphome/panel-cam-base.yaml]
       refresh: always
   ```

   So there is no second file to copy or keep in sync. `refresh: always` is
   what makes a change in the repository reach your next build — without it
   ESPHome reuses a cached copy for a day. Pin `ref` to a tag if you would
   rather updates be deliberate, or replace the whole block with
   `cam: !include panel-cam-base.yaml` to work against a local copy.

The repository's [README](https://github.com/ambient-home-systems/XIAO-ESP32S3-Sense-Speedqueen#step-1--flash-the-camera-node)
has this with every click spelled out.

### Getting firmware onto a new board the first time

Only the first flash needs a cable; everything after it goes over the air.

If the board is plugged into the Home Assistant host, the ESPHome add-on
flashes it directly. If it isn't — the usual case, since the board ends up in
the laundry room — build the firmware in ESPHome, choose **Manual download**,
pick the **Factory format** `.bin`, then open
[web.esphome.io](https://web.esphome.io) in Chrome or Edge, plug the board
into that machine and install the file. No drivers and nothing to install:
the XIAO ESP32S3 has native USB.

If no serial port appears, hold the **BOOT** button while plugging the board
in, which forces it into download mode.

### Wi-Fi without putting credentials in the firmware

Each node file ships with a `wifi:` block reading `wifi_ssid` and
`wifi_password` from your secrets. Delete that block and the node comes up as
its own open hotspot named after the device (`washer-cam`), serving a page
that asks which network to join. What you enter is saved to the board and
survives later updates.

That fallback hotspot is always there, credentials or not. A node that cannot
reach your network — because the Wi-Fi changed, say — puts it up rather than
sitting dark, so it never needs another trip to a USB cable.

### Mount before you calibrate

The ROI coordinates are tied to the exact camera position. Aim roughly
perpendicular to the tilted panel face, hood it against room light, and check
the reflection isn't sitting on the left third of the panel where the Perm
Press and Sensing LEDs live.

If the image is upside down, set `vertical_flip` and `horizontal_mirror` to
`"true"` in the node's substitutions — both together is 180°. The sensor
cannot rotate 90°; that needs the camera mounted square.

**The ▲▼ anchors are printed ink, not lights.** They have no illumination of
their own, so a hood that seals the panel off completely removes the light
they depend on, and the short exposure the segments need can leave them too
dark to find. If an anchor can't be located the add-on logs it and raises
`decode_problem`. Anchors are optional: calibrate with fewer than two and the
add-on logs `drift correction disabled` and decodes normally, losing only the
automatic correction for a nudged camera. On a DR7 they sit beside the
display; on a TR7, to the right of the two digits.

**A camera that moved further is found again.** Anchors only search a few
dozen pixels around where they were calibrated. When a frame reads as nothing
at all — no indicator lit and nothing on the display — the anchors are lost,
or the display has shown an unknown glyph three reads running, the add-on tries the whole calibration
shifted across the frame (up to a quarter of its width and 40% of its height
either way) and moves to the one place that reads a believable panel: a
character on the display, at least three indicators lit across two or more
exclusive groups (the selections a panel that is on always shows) with at most
one light per group, and more indicators lit together there than anywhere
else. It logs
`Panel moved in the camera frame - realigned by …` and the debug image shows
`shift +x,+y` beside the read. A dryer that is really off reads nothing at any
shift, so nothing moves; a frame that matches in two places is left alone with
a warning. It searches at most every five minutes per machine, and the shift
lasts until the calibration is reloaded, when it is found again on the first
dark frame. Moves of under 8 px are ignored: every region already tolerates that much.
It corrects a translation only — a camera that also rotated or
zoomed needs recalibrating — and works with or without anchors (anchors still
fine-tune after it). Recalibrate if the log shows it happening often.

### Then tune the exposure

Don't edit YAML for this. The camera's device page in Home Assistant carries
sliders for **Exposure**, **Exposure level**, **Contrast**, **Brightness**,
**Saturation** and **Gain**, and they take effect on the next frame. Drag,
reload `http://dryer-cam.local:8081/`, look.

You want lit segments clearly bright but **not** clipped to solid white, with
a visible dark gap between neighbouring segments. Too high and they bloom
together; too low and you'll catch the display mid-refresh with segments
missing. Start at 300. Each panel sits in its own light, so the two nodes will
usually end up on different values.

Those sliders are **remembered**: what you set is saved on the board,
re-applied at boot, and survives a power cut. The node's file holds the
factory defaults, used on a first boot or if nothing has been saved yet.

Saving alone would not have been enough. The component restores a number and
publishes it, but never runs the action attached to it, so the slider would
have read one value while the sensor ran another. An `on_boot` handler
re-applies whatever was restored once the entities and the camera are both
up, and logs `Applied saved camera settings` with the values — which is how
you confirm the two agree.

## 2. Install the add-on

If you are reading this in the add-on's documentation tab, it is already
installed — go to Configuration below.

Otherwise add this repository under **Settings → Add-ons → Add-on Store → ⋮ →
Repositories**:

```
https://github.com/ambient-home-systems/XIAO-ESP32S3-Sense-Speedqueen
```

"Speed Queen panel reader" then appears in the store and builds locally on
first install, which takes a few minutes.

Requires the Mosquitto broker add-on — credentials come from the Supervisor
MQTT service, so there's nothing to configure by hand.

Configure the machines below and start it. A machine whose calibration file
does not exist yet logs that it is waiting and keeps running, which is what
lets you calibrate from the Web UI (next section) before any file exists.

The repository's [README](https://github.com/ambient-home-systems/XIAO-ESP32S3-Sense-Speedqueen#setting-it-up)
walks the whole build end to end, in order, with what to check at each step.

## 3. Calibrate, once per machine

**Click Open Web UI on this add-on's page.** The calibration tool runs inside
Home Assistant — there is nothing to download, and it can pull frames from
your cameras itself.

Pick the machine under "Home Assistant" first: that sets both the indicator
list and the colour channel, which differs between the two machines. Then
**Grab frame** several times across different machine states — idle, mid-cycle,
and the end of a cycle. Four is a sensible minimum. Every indicator needs to
appear both lit and unlit somewhere across the set, and every segment needs to
appear both ways too (running through 0-9 on the time display covers all
seven).

Then:

1. **Straighten** (optional) — for a panel filmed at an angle: click the
   top-left, top-right, bottom-right and bottom-left corners of the panel face
   (or of just the display). As soon as the fourth corner is placed, every
   frame is shown perspective-corrected onto a square-on rectangle, the other
   steps work on that, and the calibration file
   records it as `warp` so the add-on straightens each snapshot the same way.
   Use it when the digit boxes cannot sit squarely on leaning segments — a
   rectangle over a slanted segment half-misses it, and lit and unlit
   segments stop separating. To move a corner later, pick it in the list and
   click its new place on the camera frame: everything already placed is
   carried across and stays on the same spot of the panel.
2. **Anchors** — click the centre of the ▲ and the ▼ next to the display.
   Skip them when the two triangles sit close together in the frame (a
   tightly cropped snapshot): over a short baseline a pixel of error becomes a
   large scale error, and a fixed camera does not need drift correction.
3. **Indicators** — click each LED centre in the order prompted.
4. **Digits** — box each digit tightly around its outer segments. The
   thresholds the tool finds for the display are a floor: while the display
   shows contrast, the add-on reads each segment against the brightest one in
   the same frame (lit at 75% of it), because a display's brightness and glow
   drift with the time of day. To change that share add `"digit_ratio": 0.7`
   (for example) to the calibration file; `0` uses the fixed thresholds only.
   An italic display can also take `"digit_slant": 10` (degrees, top leaning
   right), or `"slant"` on one digit: each segment box is then read as a
   parallelogram leaning the same way. `api/fit/<id>` finds it from the saved
   frames, and the tool shows it and sets it per digit (pick the digit in the
   list).
   Likewise a segment may carry its own `"ratio"` (its own cut, as a share of
   the brightest segment), which the fit learns for a segment the shared one
   reads badly - the middle bar of a "0", glowing inside a ring of lit
   segments, is the usual one.

Circles and boxes turn cyan when the tool reads them as lit, amber when dark.
Flip between frames to confirm they follow the real state.

### Changing one item without starting over

Opened from the add-on, every change is one click from saved: the **Unsaved changes** bar at the top of the panel and the **Save** section at the bottom both have **Save to Home Assistant** (0.8.9) - no file to copy. **Edit saved calibration** opens the machine's current file (or open a `.json`
with **Edit a calibration file**, or paste one and **Load from text**). Every
straighten corner, anchor, indicator, digit and digit segment is listed on the
right with its reading and threshold. Pick one, in the list or by clicking it
on the frame, and change only that one:

- click an empty spot to move it there, drag it, or nudge it with the arrow
  keys (Shift = 5 px); a segment box resizes with Alt + arrows;
- a digit can be boxed again (its seven segments are placed anew) and given its
  own slant;
- an indicator or segment can take a threshold typed in by hand.

Everything you don't touch is saved exactly as it was, thresholds included,
and so are fields the tool doesn't edit (a segment's `ratio`, `digit_ratio`).
A moved item's threshold is worked out again from the loaded frames, and kept
if they don't show it both lit and dark. **Undo** steps back through any
change.

**Save to Home Assistant** writes to that machine's configured
`calibration_path` and the add-on picks it up on its next poll — no file to
move, no restart, and the entities re-announce themselves if the indicator set
changed. If any region never changed state across your frames, it says so and
asks before saving; capture more frames rather than accepting a guess.

### Why the add-on fetches the frames

ESPHome's camera serves its snapshot without an `Access-Control-Allow-Origin`
header, so a browser cannot read those pixels directly — `fetch` is refused,
and drawing the image taints the canvas, which is exactly what the sampling
needs. The add-on fetching server-side and handing the bytes back on its own
origin is what makes "Grab frame" possible at all. It also sidesteps a Home
Assistant on https being unable to talk to a camera on http.

### Without the add-on

`sq-calibrate.html` is still one file with no dependencies. Open it from
`file://`, load snapshots you saved by hand, and use **Build calibration file**
and **Copy** to place the JSON yourself — the Home Assistant panel simply
doesn't appear. Save each machine's file somewhere under `/config`, one file
per machine; `/config/speedqueen_panel/dr7.json` and `.../tr7.json` match the
defaults.

The exported file records which machine it was built for, and both the add-on
and the save endpoint refuse it if that doesn't match the configured `type`.

## Configuration

```yaml
machines:
  - type: dr7
    snapshot_url: "http://dryer-cam.local:8081/"
    calibration_path: "/homeassistant/speedqueen_panel/dr7.json"
  - type: tr7
    snapshot_url: "http://washer-cam.local:8081/"
    calibration_path: "/homeassistant/speedqueen_panel/tr7.json"
publish_debug_image: true
log_level: info
```

Per machine:

| Key | Required | What it does |
|---|---|---|
| `type` | yes | `dr7` or `tr7`. Picks the indicator profile. |
| `snapshot_url` | one of | That machine's camera snapshot endpoint. |
| `camera_entity` | one of | A Home Assistant camera to read instead, e.g. `camera.washer_panel` — fetched live through HA, so every read and every **Grab frame** is current. Use either this or `snapshot_url`. A [proxy camera](https://www.home-assistant.io/integrations/proxy/) in `mode: crop` makes a panel-sized crop of a room camera. |
| `calibration_path` | yes | That machine's calibration file. |
| `id` | no | Topic and unique-ID prefix. Defaults to `type`; set it only to run two machines of the same type, and never change it afterwards — it is the entity identity. |
| `name` | no | Device name in Home Assistant. Defaults to e.g. "Speed Queen DR7". Renaming later won't rename entities that already exist. |
| `poll_active` | no | Seconds between reads while the panel is lit (default 10). |
| `poll_idle` | no | Seconds between reads while it's dark (default 60). |
| `keep_frames` | no | How many frames to keep for checking the calibration (default 200, `0` = none). See [Saved frames](#saved-frames). |

## When the camera moves

A camera that is bumped, or a pan-tilt camera that comes back to its position
a little off, moves the panel in the frame. Each machine keeps a **reference
picture** of its panel (`<id>.reference.jpg` beside the calibration) and the
placement it was read at, and every 10 minutes - panel on or off - and every
minute while the reads look lost, it finds the panel again by matching the
outlines of the printed panel against that picture. The log says so:

```
[tr7] Panel moved in the camera frame - placed at +72, -5 px by the reference (was +0, +0; match 0.55, lead 1.61)
```

The reference is taken automatically from the first clean read the countdown
confirms, and refreshed daily. To set it yourself - after fixing a placement,
or after sticking two small pieces of **retroreflective tape** beside the
panel, which the camera's infrared turns into the sharpest marks in the frame:

- `POST api/reference/<id>` — make the current frame the reference.
- `GET api/register/<id>` — find the panel against it now; shows the match.

### Editing a calibration after the camera moved

The reader copes with a moved camera, but the calibration file still holds the
corners it was made with, and the calibration tool draws the file on today's
frame - so after a move every item shows up off by the same amount. When the
reader has placed the panel away from where the file says, the tool shows
**Rebase to camera** under the Home Assistant buttons, with the offset. It moves
the file's four corners to where the camera sees the panel now (and the
reference picture's offset back by the same amount, so nothing is shifted
twice), keeps the previous file as `<id>.before_rebase.json`, and opens the
moved file on a fresh frame. The reader samples the same spots before and after.
Rebase first, then fix whatever was really misplaced.

- `POST api/rebase/<id>` — the same, without the tool.

## AI reader (optional)

Off by default. When configured, a vision model reads the same frame as the
decoder - every `every` seconds while a machine runs, and whenever the decoder
is unsure (an unknown glyph, a jump the countdown rules out, a lost panel).
When the two agree, that's it. When they don't, the answer the countdown
expects stands; when both or neither fit, a second model (if configured)
breaks the tie by majority. The display Home Assistant gets is always the
settled one; the **Read by** entity says which reader it came from.

```yaml
ai:
  provider: home_assistant              # or openai_compatible, or none
  entity_id: ai_task.panel_reader       # an AI Task entity (home_assistant)
  escalate_entity_id: ai_task.claude_vision   # optional: breaks ties
  every: 60                             # seconds between reads while running; 0 = only when unsure
  max_calls_per_day: 500                # all machines together
```

```yaml
ai:
  provider: openai_compatible
  base_url: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
  api_key: "sk-..."
  model: qwen-vl-max                    # the model must accept images
  escalate_model: qwen-vl-max           # optional
```

| Key | What it does |
|---|---|
| `provider` | `none`, `home_assistant` (an AI Task entity: any model a Home Assistant integration offers, with HA metering its tokens) or `openai_compatible` (`{base_url}/chat/completions`: OpenAI, Alibaba Model Studio / Qwen, OpenRouter, Ollama ...). |
| `entity_id`, `escalate_entity_id` | The AI Task entities, for `home_assistant`. The frame is handed over as a file under `/media/speedqueen_panel/`. |
| `base_url`, `api_key`, `model`, `escalate_model` | For `openai_compatible`. |
| `every` | Seconds between reads while a machine runs (default 60). `0` asks only when the decoder is unsure. |
| `max_calls_per_day` | A cap on model calls per day, all machines (default 500). After it, the decoder reads alone until midnight. The day's count survives an add-on restart (0.8.5). |
| `timeout` | Seconds to wait for an answer (default 45). A slow or failed call is no answer; the decoder's read is published. |

**Try a model on your own frames before trusting it.** Over ingress:

- `GET api/ai/bench/<id>?n=20` — the newest labeled saved frame of each
  display value, read by the model: how many it read right, its median time
  and its tokens. `&escalate=1` tries the second model.
- `GET api/ai/read/<id>` — the current frame, read by the decoder and the model.
- `GET api/ai/status` — the settings and today's calls, tokens and errors.

New diagnostic entities per machine: **Read by** (panel / ai / both), **AI
display** and **AI reads today**. And one switch for all machines,
`switch.speed_queen_ai_reader` (0.8.2): off, the model is not asked and the
decoder reads alone, until it is switched on again - restarts keep it off.
Manual `api/ai/read` and `api/ai/bench` calls still run.

Every model read keeps the frame it was given (0.8.3) in
`/media/speedqueen_panel/checks/`, named by time and machine, with a `.json`
of what each reader said - the newest 300 at most (about 15 MB), none older
than 7 days. **AI display** carries the newest one's path as its `frame`
attribute on the poll that asked.

## What you get

One device per machine. For a DR7 configured with the default `id`:

- `sensor.speed_queen_dr7_display` — raw decode, including letter codes
- `sensor.speed_queen_dr7_time_remaining` — minutes, unavailable during a code
- `sensor.speed_queen_dr7_state` — off / ready / running / cooling / done / fault
- `sensor.*_cycle`, `*_temp`, `*_dryness` — collapsed, one lit LED each
- 13 binary sensors — status, options, alerts
- `binary_sensor.*_decode_problem` — anchors lost, an unknown segment pattern, or a blocked view
- `sensor.*_reader` — `ok`, `misread` (a "?" or lost anchors) or `hidden` (the view is blocked — a TR7 lid stood open in front of its console, say)
- `camera.*_panel` — the annotated frame, for re-aiming

A TR7 is the same shape: `*_cycle`, `*_temp`, `*_load_size` and `*_soil`
collapsed, 11 binary sensors (Wash, Rinse, Spin, the six options, and the two
locks), and a state of off / ready / running / fault — no `cooling`, and no
`done`, as above.

## Notes

- Polling drops to 60s when a panel is dark, 10s when anything is lit, per
  machine. This is mostly about keeping the XIAO cool.
- Alert on `sensor.*_reader` being `misread` for a few minutes. Without it, a
  bumped camera silently freezes every other entity at its last value. A
  camera that moved without turning is usually realigned within five minutes
  (see above). `decode_problem` is also on while the view is blocked, which on
  a TR7 is whenever the lid stands open — `reader` tells that apart as
  `hidden`.
- **One borderline segment is read through.** A digit whose seven segments
  make no glyph, but would with one segment that reads within 10% of the
  display's spread of its cut, is read as that digit — if exactly one segment
  does it, one does it clearly nearer its cut, or the countdown expects it.
  `raw` keeps what the segments literally said, the debug image marks the
  segment in magenta, and the log says which one.
- **A surprise gets a closer look.** A display that reads "?", or changes to
  anything but itself or one minute less, is read again — up to three frames
  a second apart — and believed once two agree or one reads what the
  countdown expects. While a countdown is ticking, a read one borderline
  segment away from the expected minute is read as that minute, and a read
  more than 5 minutes above it is reported as a misread for up to five
  minutes (a countdown doesn't leap up; a real change outlasts that). A failed
  snapshot is tried once more before the machine is marked offline.
- A fault code on the display sets `state: fault` but does **not** set
  `decode_problem` — that's a correct read of a real condition.
- Each machine has its own availability topic alongside a shared one for the
  add-on. One camera dropping off marks only that machine unavailable.
- A missing calibration file takes only its own machine out; the others keep
  publishing, and it's picked up as soon as the file appears.

## Saved frames

A calibration is only as good as the frames it was checked on, and the frames
that matter are the ones that went wrong. So the add-on keeps them, in a
`frames/<id>/` folder next to each calibration file
(`/homeassistant/speedqueen_panel/frames/dr7/` for the defaults): the camera's
JPEG as fetched and a `.json` beside it with what was read, every segment's
level and cut, and a **label** — what the display really showed:

| Kind | When | Label |
|---|---|---|
| `misread` | still "?" after a closer look | from the clean reads either side, once the next one comes: of the two, the one its squarely-read segments agree with |
| `resolved` | a digit read through a borderline segment | the same way, so a wrong guess shows up |
| `disagreed` | a frame a closer look overruled | what the frames that agreed read |
| `sample` | a clean read, once per display value every six hours | itself |

`keep_frames` caps the folder (oldest first). Nothing in the decode path ever
reads it.

**Every calibration that loads is checked against the labeled frames**, and the
log says so:

```
[dr7] Calibration check: 17 of 18 saved frames read right (18 with a borderline
segment resolved), weakest digit 2 d dark (-0 levels); misread: '14' as '1?'
(20261012-180001.204-misread); not seen dark yet: digit 2 c
```

The same is available on request, over ingress like the rest of the Web UI:

- `api/frames/<id>` — what is saved, and the last check.
- `api/check/<id>` — check the calibration file as it is now; or POST a
  calibration to it to check that one without saving it.
- `api/fit/<id>` — each digit segment box that reads its frames narrowly,
  tried up to 4 px either way and moved where it reads them most squarely.
  Only segments seen both lit and dark move. Returns the moves, the check
  before and after, and the whole proposed calibration — it saves nothing; to
  use it, save that calibration through the tool or `api/calibration/<id>`.
