# Changelog

## 0.8.8

- **Rebase to camera.** After the camera moves, the reader finds the panel by
  its reference picture and reads it that far from the calibration - but the
  file keeps the old corners, so the calibration tool drew every item off by
  the same amount (on 2026-10-27 the washer's +50 px made nothing line up).
  When the reader's placement isn't 0, 0 the tool now shows **Rebase to
  camera** with the offset: it moves the file's four warp corners there, takes
  the same amount off the reference picture's offset (so the reload doesn't
  shift twice), keeps the old file as `<id>.before_rebase.json` and opens the
  moved file on a fresh frame. "Edit saved calibration" warns when the file is
  out of line. New: `POST api/rebase/<id>`; `api/machines` reports each
  reader's `offset`.

## 0.8.7

- **No more "non-numeric value 'unknown'" errors for Time remaining.** The
  discovery template rendered the text `unknown` whenever the display showed
  no time (idle, Done, a fault), and Home Assistant rejects text for a
  duration sensor - about 60 errors a week in the core log, one on every
  blank display and every availability message. It now renders `None`, which
  Home Assistant stores as the unknown state.

## 0.8.6

- **A held AI display is checked again after 20 s, not at the next regular
  read.** Once the model overrules a guessed digit, its display is held while
  the decoder keeps guessing something else. At the start of a washer load
  that hold could last a whole `every` (5 minutes here): on 2026-10-21 the TR7
  re-estimated its time during Auto Fill (49, 45, 46, 47, 44, 43), the model
  read 49, and because no countdown tick had been seen yet the decoder's
  different (and correct) reads did not count as unsure - Home Assistant
  showed 49 from 11:02 to 11:07, then jumped to 42. A standing hold now counts
  as unsure, so the model is asked again after `AI_UNSURE_GAP` (20 s) and
  follows the panel. It costs a model call only while the decoder and the
  held display disagree.

## 0.8.5

- **Today's AI counts survive a restart.** Calls, tie-breaks, errors and
  tokens were kept in memory only, so every add-on restart (an update, a
  Supervisor restart) zeroed **AI reads today** - and gave the day a fresh
  `max_calls_per_day`. They are saved to `/data/ai_stats.json` after every
  model call and read back on start; a file from an earlier day is ignored.
- **The AI reader is told about leading zeros.** On 2026-10-19 Claude Haiku
  answered "?3" for a dryer's "03": the leading 0 glowed dimmer and softer than
  the 3 and it would not commit to it (the answer is ignored, so nothing went
  wrong, but the check was wasted). The prompt now says that under 10 minutes
  the left position shows a 0, often dimmer, and to read it as 0 when its ring
  of bars is there.

## 0.8.4

- **The calibration tool edits an existing calibration item by item.** Open
  the saved file (**Edit saved calibration**, new `GET api/calibration/<id>`),
  a `.json`, or pasted text; every straighten corner, anchor, indicator, digit
  and segment is listed on the right with its reading and threshold, and any
  one can be moved (click, drag, arrow keys), a segment resized, a digit
  re-boxed or given its own slant, a threshold set by hand - without touching
  the rest. Untouched items, their thresholds and fields the tool does not
  edit (`ratio`, `digit_ratio`, a calibration's straightened size) are saved
  exactly as loaded; an opened file exports byte-for-byte equal in content.
  Moving a straighten corner carries everything placed onto the new
  straightening instead of clearing it. **Undo** undoes any change.
- **A misread that looks like a fault code asks the AI at once.** On
  2026-10-19 the washer's 30 read as "3U" (the top bar of the 0 missed); a
  letter reads as a fault code, a fault is not "active", and the AI reader
  waited its idle gap (15 minutes) while Home Assistant showed a fault for a
  minute. A countdown that was running moments ago now counts as running, so
  anything it did not expect is checked within 20 s - in practice on the same
  poll, before it is published.

## 0.8.3

- **A guessed digit can no longer beat the AI.** On 2026-10-19 the washer's
  first digit read "?", and a closer look with a nudged box read its 6 as a 5:
  69 showed as 59. Two frames agreed, so the surprise counted as settled and
  the model was not asked; five minutes later the model read 67, but the
  countdown - now built on the misread - expected 57 and threw it out. The
  wrong tens digit stood for the rest of the load. Now any display the
  countdown did not expect asks the model, settled or not; a read with a
  resolved digit never counts as fitting the countdown against the model (a
  disagreement it can't settle goes to the second model); and after the model
  overrules a resolved read, its display is held while the decoder keeps
  guessing, until the decoder reads cleanly or agrees.
- **Every AI check keeps the picture the model was given**, so what it saw
  can be looked at afterwards: `/media/speedqueen_panel/checks/<YYYYmmdd-HHMMSS>_<id>.jpg`
  plus a `.json` of what each reader said (decoder, model, second model,
  what was shown, Read by). Bounded: the newest 300 pictures (about 15 MB),
  none older than 7 days, pruned after every save.
- The **AI display** sensor gains attributes `frame` (that picture's path
  under the media root - `media-source://media_source/local/<frame>`),
  `model` and `second`, set on the poll that asked the model and null
  otherwise.

## 0.8.2

- **An "AI reader" switch in Home Assistant** (`switch.speed_queen_ai_reader`,
  on a "Speed Queen panel reader" device) turns the model off and on without
  editing the add-on's options or restarting it - to stop the spending for a
  while, or rule the model out when something reads oddly. Off, the decoder
  reads alone (Read by stays "panel"); the setting survives restarts. A read
  asked for through `api/ai/read` or `api/ai/bench` still runs.
  `api/ai/status` reports it as `paused`. Announced only when a model is
  configured; with `provider: none` a switch left from before is removed.

## 0.8.1

- **Read by no longer flickers.** The decoder reads every 10 s and the model
  every 60, so a machine's Read by flipped panel / both / panel every minute.
  Between model reads it now stays "both" while the machine runs and the
  decoder keeps reading cleanly after the model last agreed (or was adopted).
- **A reference picture of a blocked view is refused.** `POST
  api/reference/<id>` now checks the frame against the current reference (or,
  with none yet, that the panel is not blocked) - an open TR7 lid hides the
  whole console, and such a frame was saved once. `?force=1` saves it anyway.

## 0.8.0

On 2026-10-18 the washer started and the reader reported a fault for the whole
load. The laundry camera had moved about 72 px since the calibration. The
search for a moved panel found it, but one LED column too far (+121 px): the
LED grid repeats about every 45 px, so a placement one column over still lit
enough indicators to look believable, and every display read after that was
"?".

- **A moved panel is placed by a reference picture.** Each machine keeps a
  picture of its panel (`<id>.reference.jpg` beside the calibration) and the
  placement it was read at. Every 10 minutes - panel on or off - and every
  minute while reads look lost, the frame's edge map is matched against the
  reference's (normalised cross-correlation, FFT, ~0.2 s). Printed buttons,
  labels and the bezel correlate wherever the panel sits, so the match needs
  nothing lit and does not slip a grid column. On the real frames it put the
  washer at +72, -5 and the dryer at +76, 0, both to the pixel. The old search
  is only used while a machine has no reference yet.
- **The reference keeps itself fresh.** A clean read that the countdown (or
  the AI reader) confirms replaces it once a day. `POST api/reference/<id>`
  makes the current frame the reference at once - after placing the panel by
  hand, or after sticking retroreflective markers beside the panel, which the
  camera's infrared makes the brightest edges in the frame.
- **A digit that reads as no glyph is read again a few pixels either way.**
  The digit most placements within 3 px read, weighted by how squarely each
  reads, wins - a digit only, and only with 70% of the weight.
- **An optional AI reader.** Off unless the new `ai` option names a provider:
  a Home Assistant AI Task entity (any model an HA integration offers), or any
  OpenAI-compatible endpoint (OpenAI, Alibaba Model Studio / Qwen, OpenRouter,
  Ollama ...). It reads the same frame every `every` seconds while a machine
  runs and whenever the decoder is unsure. Agreement settles it; otherwise the
  answer the countdown expects stands, and a configured second model breaks a
  tie by majority. A model answer adopted twice in a row places the panel
  again. New diagnostic entities: Read by (panel / ai / both), AI display, AI
  reads today. `max_calls_per_day` caps the cost.
- **Compare models on your own frames.** `GET api/ai/bench/<id>?n=20` sends
  the newest labeled frame of each display value to the configured model and
  reports how many it read right, its median time and its tokens;
  `&escalate=1` tries the second model. `GET api/ai/read/<id>` reads the
  current frame with both the decoder and the model.
- On 58 labeled frames from this laundry room: Claude Haiku read 54 of 57 (the
  misses were blurred frames), Claude Sonnet 57 of 57.

## 0.7.3

On 2026-10-15 Home Assistant restarted at 03:00 and both machines stayed
unavailable all day, although the add-on kept reading the panels. The broker
had dropped the add-on's connection during the restart and published its last
will, `speedqueen/panel/availability` = offline (retained). paho reconnected by
itself, but the add-on only claimed "online" once, at start - and every entity
needs that topic AND its machine's to be online.

- **Every connect claims the add-on's availability again**, the first one and
  every reconnect after a broker restart or a network drop.
- **Home Assistant's birth message re-announces everything.** When
  `homeassistant/status` says online, each machine republishes its discovery
  and state on the next poll instead of relying on retained messages alone.

## 0.7.2

The end of the same dryer load: from 10 minutes left the display read 10, 09,
08 ... 02, and the reader published 18, 89, 88 ... 82. With the room light on,
the inside of the leading "0" glowed like its dark middle bar (about 130
against a cut of about 126), and the countdown snap then pulled two correct
reads (06, 03) to 86 and 83.

- **Each segment can have its own cut.** A segment's `ratio` in the
  calibration overrides `digit_ratio` for it. `api/fit/<id>` learns it from the
  labeled frames, only for segments the shared cut serves badly: midway between
  that segment's dimmest lit read and brightest dark read, as shares of each
  frame's brightest segment. On the 69 labeled DR7 frames from that night, the
  shared 0.75 read 52 and published 8 wrong numbers; per-segment cuts, learned
  without the frame being judged, read 59 with no wrong number.
- **A countdown doesn't leap up.** While one has ticked in the last five
  minutes, a read more than 5 minutes above the last one is reported as a
  misread (no time remaining, decode problem on, so Home Assistant holds its
  own countdown) for up to five minutes. A real jump outlasts that. Replayed
  over that stretch with the old calibration, no wrong minute is published.
- **A "?" is no longer a fault.** The state comes from the lights, as for any
  other read; only a code like `do` or `nH` is a fault.
- **A calibration saved while a read is in progress** no longer crashes that
  read ("NoneType has no attribute chosen").

## 0.7.1

From the first real dryer load on 0.7.0 (2026-10-12, the room light switched
on and off to test both):

- **The countdown survives a calibration reload.** Saving a calibration
  mid-cycle used to start the reader over, so a "6" whose dark segment b
  caught glow read 26 as 28 - a valid glyph, read the same twice - and stood.
  The last read and the last tick now carry over, and the snap to the
  expected minute catches it: tonight's 20-read-as-28 frame, replayed across
  a reload, publishes 20.
- **A wrong read no longer labels itself.** A clean read is kept as a sample
  only when the countdown agrees with it (the same minute or one less). A
  countdown that jumps and holds is kept as `surprise`, unlabeled, for a look.
  A misread is labeled from the reads either side only when those are one
  minute apart - tonight a misread between 23 and 21 was labeled 23.
- **Slanted segment boxes.** A calibration may set `digit_slant` (degrees,
  positive = top leans right), or `slant` on one digit, and each segment box
  is sampled as a parallelogram leaning that way about its own centre. The
  fit tries each digit's slant (-6 to +20 degrees) before it moves boxes. On
  43 labeled DR7 frames it leaned the first digit and read 42 instead of 41;
  the one left, a "0" whose middle glows as bright as its dark bar, is what
  the countdown snap is for. The calibration tool doesn't know the field yet:
  re-saving from the tool drops it.
- The checks and fits no longer log "Expected 2 anchors" for every file they
  read.

## 0.7.0

- **A digit with one borderline segment is read, not shown as "?".** Nearly
  every "?" on the real panels was one segment box a few px off its bar, so
  that segment read within a few levels of its cut while the other six read
  squarely. Now that pattern reads as the digit one flip away, if exactly one
  segment within 10% of the display's spread gives a digit (or one gives a
  digit clearly nearer its cut, or the countdown names it). On today's real
  frames read with each day's older calibration this fixed 3 of 4 dryer and
  both washer "?" reads, and never read a wrong digit. `raw` still carries
  what the segments literally said; the debug image marks a resolved segment
  in magenta.
- **A surprising display gets a closer look.** A "?", or a display that
  changed to anything but itself or one minute less, is read again (up to
  three frames a second apart) and believed when two agree or one reads what
  the countdown expects. And while a countdown is ticking, a read one
  borderline segment from the expected minute - the 6 whose segment e read
  68 as 58 frame after frame - is read as that minute. A display that is not
  counting down (a knob turned on a ready machine) is never pulled back.
- **A failed snapshot is tried once more** after two seconds before the
  machine is marked offline. A room camera behind Home Assistant now and then
  answers 500 or times out once.
- **New `sensor.*_reader`: ok / misread / hidden.** `decode_problem` is on
  both for a misread and for a blocked view, and a TR7's lid stood open hides
  its console most of the day - so an alert on it fired constantly. `reader`
  tells the two apart: alert on `misread`.
- **Frames worth keeping are saved beside the calibration**
  (`frames/<id>/` next to the calibration file): misreads, resolved reads,
  frames a closer look overruled, and one clean read of each display value
  every six hours, each with what the display really showed - a misread is
  labeled from the reads either side of it. `keep_frames` (per machine,
  default 200, 0 = off) caps them.
- **Every calibration that loads is checked against them**, and the log says
  how many frames read right, the narrowest segment margin, which frames
  misread and which segments have never been seen lit or dark.
- **New API routes** (ingress, like the rest): `api/frames/<id>` (what is
  saved, and the last check), `api/check/<id>` (check the file on disk, or
  POST a calibration to check it without saving), and `api/fit/<id>` (digit
  boxes moved to where they read the saved frames best - a proposal, saves
  nothing). Fed today's frames, the fit found the same box moves made by hand
  that day.

## 0.6.2

- **A misread digit no longer moves the calibration.** On a running TR7 one
  frame's unknown glyph started a search, which adopted a 4 px shift; with it
  the washer's first digit misread more often and the machine dropped to
  `fault`. A "?" now starts a search only after three unreadable reads in a row
  (a misread comes and goes; a moved camera stays unreadable), and a shift under
  8 px of the current one is ignored. Dark frames and lost anchors still search
  at once.

## 0.6.1

- **A dark panel no longer "realigns" onto its own printing.** On the first
  real run of 0.6.0 the dryer had just finished and its panel was dark; under
  the room camera's infrared the printed ▲▼ triangles read as a "1" and a bezel
  glint as one lit indicator, and the add-on shifted the calibration 88 px onto
  them. A shift now needs what a panel that is on always shows - at least three
  indicators lit across at least two exclusive groups (its selected cycle,
  temperature, dryness or load and soil). A wrong shift from 0.6.0 is cleared by
  this update's restart.

## 0.6.0

- **A camera that moved is found again.** On a real DR7 the room camera's
  picture shifted about 60 px, every region landed on blank panel, and the
  dryer read as off mid-cycle — further than anchors can search, and the
  calibration had none (the ▲▼ sit too close together in a tight crop). Now a
  frame that reads as nothing at all, an unknown glyph or lost anchors makes
  the add-on try the whole calibration shifted across the frame, at most every
  five minutes per machine. It adopts the one place where the most indicators
  light together in a believable pattern (a character on the display, one light
  per exclusive group), settles it to the pixel where the display's segments read
  most squarely, and logs the shift; the debug image shows it too. A panel that
  is really off, or a frame that matches in two places, never moves the
  calibration. Tested on the real frames from that day: both realign to within
  2 px of the hand-measured shift and read the display and every light.

## 0.5.0

- **Digits survive the display's brightness changing.** A display's brightness
  and the glow it throws onto unlit segments drift with the time of day and the
  camera's exposure — on a real DR7 the lit segments read 158-190 in one hour
  and 214-229 the next, with unlit neighbours glowing up to 94 and then 135, so
  any fixed per-segment threshold eventually misread a digit. Segments are now
  judged against the brightest segment in the same frame (lit at 75% of it by
  default); the calibrated thresholds remain the floor, and the only rule for a
  dark or fully lit display. A calibration file can set `digit_ratio` (0 turns
  this off).
- **A blocked view is a decode problem, not a reading.** A frame where someone
  stands in front of the camera can wash every indicator "lit" — including a
  dryer's Complete. More than two lit indicators in one exclusive group (cycle,
  temperature, dryness, load size, soil) cannot happen on a real panel, so such
  a frame now sets `decode_problem` instead of passing as a valid state.

## 0.4.0

- **Read a Home Assistant camera directly.** A machine can name a
  `camera_entity` instead of a `snapshot_url`; the add-on fetches that camera's
  current picture through Home Assistant's API, so every read and every
  **Grab frame** is live — no snapshot files to keep fresh. Pair it with a
  proxy camera in `mode: crop` to give each machine a panel-sized crop of one
  room camera. The add-on now asks for Home Assistant API access
  (`homeassistant_api: true`) for this; the token is only ever sent to Home
  Assistant's own API, never to a `snapshot_url`.
- **The calibration tool warns about anchors placed too close together**
  (under 60 px): over so short a baseline a pixel of error misplaces every
  region. The cue says so and saving asks first; skipping anchors is fine for
  a fixed camera.

## 0.3.8

- **Straighten shows the result straight away.** After the fourth corner the
  Straighten step itself now displays the straightened, square-on frame; it
  used to keep showing the raw (still crooked) frame until you moved on to
  another step, which looked like nothing had happened. Undo point returns to
  the raw frame to move a corner.

## 0.3.7

- **Straighten a panel filmed at an angle.** The calibration tool has a new
  first step: click the four corners of the panel face (or the display) and
  every frame is perspective-corrected onto a square-on rectangle before
  anything is placed. The calibration file records it as `warp` (the four
  corners and the straightened size) and the add-on applies the same
  transform to each snapshot, so upright digit boxes fit segments that lean
  in the camera's view. Optional — a calibration without `warp` reads exactly
  as before. The tool and the decoder solve the same eight equations
  (Pillow's perspective convention), so the tool shows the frame the decoder
  samples, and the debug camera shows the straightened panel.
- The calibration UI's save refuses a malformed `warp` (not four corners, or
  corners in a line) without touching the saved file.

## 0.3.6

- **Anchors work in a tightly cropped frame.** Each ▲▼ anchor was searched for
  in a fixed 52 px box. Where the two triangles sit under ~60 px apart — a
  snapshot cropped close to the panel — each box held both, both centroids
  landed between them, and the add-on read the panel as half size
  (`Anchor scale 0.48 outside tolerance`): every region sampled the wrong spot
  and `decode_problem` stayed on. The box is now capped at 0.45x the distance
  between the anchors; wider calibrations search exactly as before.
- **A calibration file edited on disk is picked up.** The file was read once,
  so a hand edit (or one copied in) did nothing until the add-on restarted.
  It is now reloaded on the next poll when its modification time changes, and
  the entities re-announce — the same reset a save from the calibration UI does.

## 0.3.5

- **More than one machine now starts.** With two or more entries under
  `machines`, the add-on exited at once with `JSONDecodeError: Extra data`:
  bashio hands a list of objects over one per line, not as a JSON array.
  `run.sh` now gathers them into an array with `jq -s`.

## 0.3.4

Documentation only in the add-on; the camera nodes gain persistent tuning.

- **The camera's tuning sliders now survive a reboot.** What you set is saved
  on the board and re-applied at boot, so a power cut no longer costs you a
  tuning session and there is no YAML edit or reinstall to make it stick.
- Saving alone would not have worked: the number component restores a value
  and publishes it without running the action attached to it, which would have
  left a slider reading one number while the camera ran another. A boot
  handler re-applies the restored values and logs `Applied saved camera
  settings`, so the two are visibly in step.
- A node file's camera substitutions are now the **factory defaults** — used
  on a first boot, or when nothing has been saved yet.

## 0.3.3

Documentation only in the add-on; a fix to how camera nodes fetch their
shared configuration.

- **Node files now set `refresh: always` on the shared camera package.**
  ESPHome caches a fetched remote file for a day by default, so a change made
  in this repository could silently fail to reach a node's next build — which
  looks exactly like the change not working. Every build now re-fetches.
- The documentation says that the camera's tuning sliders live in the node's
  **firmware**, so a node flashed before they existed needs installing once
  more before they appear, and adds both of these to the troubleshooting
  table.

## 0.3.2

Documentation only in the add-on; the camera nodes gain live tuning controls.

- **The camera's settings are now adjustable from Home Assistant** — sliders
  for exposure, exposure level, contrast, brightness, saturation and gain on
  the camera's own device page, taking effect on the next frame. Finding an
  exposure value no longer means editing YAML and reinstalling between each
  guess. They are deliberately not persisted: every boot starts from what is
  compiled into the node's file, so the file stays the truth about what a node
  is doing, and the documentation says to write your final values back into it.
- **Flip and mirror are per-node substitutions.** Both `"true"` turns the
  image 180°, which is what an upside-down mount needs. The sensor cannot
  rotate 90° — that needs the camera mounted square.
- **The ▲▼ anchors are documented as printed ink rather than lights.** They
  are only visible by ambient light, so a hood that seals the panel off can
  take them away, and the short exposure the segments need can leave them too
  dark to find. Anchors are optional — with fewer than two the add-on logs
  `drift correction disabled` and decodes normally, losing only the correction
  for a nudged camera.

## 0.3.1

Documentation only — the add-on itself is unchanged from 0.3.0.

Flashing a camera node is now written as the clicks you actually make in the
ESPHome add-on, rather than assuming you would know how to get a file into
the ESPHome configuration directory. In particular:

- ESPHome **generates the API encryption key** when you create the device.
  The old instructions sent you to a terminal to make one, which was both
  unnecessary and a dead end for anyone without shell access.
- Where the Secrets editor lives, and that `wifi_ssid` / `wifi_password` are
  usually already there from an earlier device.
- That you replace the wizard's generated configuration wholesale, rather
  than "copying a file" somewhere unspecified.

## 0.3.0

The calibration tool is now served by the add-on. Click **Open Web UI** on the
add-on page and it opens inside Home Assistant — nothing to download, and it
can pull frames from your cameras and save the result itself.

### Added

- **The calibration tool over Home Assistant ingress.** Supervisor handles the
  authentication, so nothing is exposed outside Home Assistant.
- **Grab frame.** The tool pulls a snapshot from the machine's configured
  camera instead of you saving JPEGs by hand. This has to go through the
  add-on: ESPHome's camera serves its snapshot with no
  `Access-Control-Allow-Origin` header, so a browser cannot read those pixels
  itself — the fetch is refused, and drawing the image taints the canvas, which
  is what the sampling needs. It also sidesteps a Home Assistant on https being
  unable to reach a camera on http.
- **Save to Home Assistant.** Writes to that machine's configured
  `calibration_path`, so there is no file to move and no `/config` versus
  `/homeassistant` confusion. The machine reloads it on its next poll and
  re-announces its entities if the indicator set changed — no restart.

  The destination is always the path from the add-on's own configuration; the
  request only names a machine. An upload is refused unless it parses, is
  version 1, matches that machine's type, and has indicators and digits.

### Changed

- `sq-calibrate.html` moved into the add-on directory, because Docker cannot
  copy a file from outside the build context. It is still one file with no
  dependencies: open it from `file://` and it behaves exactly as before, with
  the Home Assistant panel simply absent. That path is not deprecated.
- The documented order changed: install and configure the add-on **before**
  calibrating, since the tool now lives inside it. A machine whose calibration
  file does not exist yet logs that it is waiting and keeps running, which is
  what makes that order work.

## 0.2.2

Documentation only — the add-on itself is unchanged since 0.2.0.

- Installing the add-on is described properly: add the repository and install
  from the store. The old instruction to copy files into `/addons` by hand
  described the local-add-on route and contradicted the repository's own
  install button.
- The mounting and exposure guidance had ended up underneath the Wi-Fi
  provisioning heading, where it read as part of it. Both have their own
  headings again.
- The repository README is now a step-by-step build guide, in order, with what
  to check at the end of each step and a table of what the common symptoms
  mean. This documentation tab stays the reference for options, entities and
  the machine profiles.

## 0.2.1

Documentation only — the add-on itself is unchanged from 0.2.0.

Flashing a camera node got simpler, and the documentation tab now covers it:

- A node file pulls the shared camera block from the repository over
  `github://`, so there is one file to copy into your ESPHome config
  directory rather than two, and no second file to keep in sync.
- Nodes carry a fallback hotspot and a captive portal. Delete a node's `wifi:`
  block and it can be provisioned from a phone instead of having credentials
  compiled in; either way, a node that later cannot reach the network puts the
  hotspot up rather than going dark, so a Wi-Fi change no longer means finding
  a USB cable.
- The first flash is documented properly: build in ESPHome, download the
  factory `.bin`, and install it from web.esphome.io in Chrome — no drivers,
  nothing to install, and only ever needed once per board.

## 0.2.0

Supports any number of machines, and the DR7 is no longer baked into the
add-on's identity. The washer profile (TR7) is new.

**This release renames the add-on, which Supervisor treats as a different
add-on.** The old "Speed Queen DR7 panel reader" (slug `dr7_panel`) will not
update into this one. To move across:

1. Note your existing options, then uninstall the old add-on.
2. Install "Speed Queen panel reader" from this repository.
3. Re-enter your settings in the new `machines` list (below).

Your Home Assistant entities survive this. A single DR7 publishes to exactly
the same topics and unique IDs as 0.1.x (`dr7/panel/…`, `speedqueen_dr7_…`),
so the existing device and its entity IDs are picked up again untouched. Your
existing `calibration.json` also still loads as-is — point `calibration_path`
at wherever it already lives, there's no need to move or rebuild it.

### Changed

- `snapshot_url`, `calibration_path`, `poll_active` and `poll_idle` moved out
  of the top level into a `machines` list, one entry per machine:

  ```yaml
  machines:
    - type: dr7
      snapshot_url: "http://dryer-cam.local:8081/"
      calibration_path: "/homeassistant/speedqueen_panel/dr7.json"
    - type: tr7
      snapshot_url: "http://washer-cam.local:8081/"
      calibration_path: "/homeassistant/speedqueen_panel/tr7.json"
  ```

  Optional per machine: `id` (the topic and unique-ID prefix, defaults to the
  type — needed only for two machines of the same type), `name` (the device
  name in Home Assistant), `poll_active`, `poll_idle`.

- Entities now carry two availability topics with `availability_mode: all`: a
  shared one for the add-on process and one per machine. One unreachable
  camera marks only its own machine unavailable instead of taking the whole
  add-on's entities down.
- A machine whose calibration file is missing is skipped with a logged error
  and retried, rather than blocking startup for every other machine.
- `state` reports `ready` when any collapsed selection is lit, not only a
  cycle. On a DR7, lighting a temperature or dryness LED without a cycle now
  reads `ready` where 0.1.x read `off`.

### Added

- `tr7` machine profile for the washer, read from a photograph of the panel:
  33 indicators, with cycle, water temperature, load size and soil level
  collapsing to one sensor each, and Wash/Rinse/Spin, six options and the two
  lock indicators staying individual.

  Two caveats worth knowing. The TR7 **samples the blue channel, not red** —
  its lit indicators are saturated blue (R≈60 G≈57 B≈243) against neutral grey
  unlit dots (≈133 everywhere), so in red a lit LED reads darker than an unlit
  one and every indicator decodes backwards. The calibration tool picks the
  channel with the machine, and the add-on warns if a file disagrees with its
  profile. And the panel has **no Complete light**, so a TR7's `state` goes
  `running` → `ready` at the end of a cycle and never reports `done`; trigger
  automations on that transition.
- The calibration tool has a machine picker that sets the click list and the
  sampling channel, writes the machine type into `calibration.json`, and ships
  each indicator's label in the file. The add-on prefers those labels, so
  correcting a legend needs only the tool.
- Startup rejects a calibration file built for a different machine type, a
  duplicate or reserved indicator name, and two machines sharing an `id`.
- `esphome/panel-cam-base.yaml` holds the camera block both nodes share;
  `dryer-cam.yaml` and `washer-cam.yaml` are thin per-node files.

## 0.1.0

First release. Decodes the 31 panel indicators and the two-digit display,
publishes over MQTT discovery, and corrects for camera drift using the
printed ▲▼ triangles as anchors.
