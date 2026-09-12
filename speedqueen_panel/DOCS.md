# Speed Queen panel reader

Reads a Speed Queen 7-series control panel with a camera and publishes about
twenty entities per machine to Home Assistant. No machine learning: indicators
are brightness thresholds, digits are seven-segment lookups.

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

Those sliders are **not** persisted across a reboot — every boot starts from
what is compiled into the node's file, so the file stays the truth about what
a node is doing. When you find values you like, write them into that file's
substitutions and install once more.

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

1. **Anchors** — click the centre of the ▲ and the ▼ next to the display.
2. **Indicators** — click each LED centre in the order prompted.
3. **Digits** — box each digit tightly around its outer segments.

Circles and boxes turn cyan when the tool reads them as lit, amber when dark.
Flip between frames to confirm they follow the real state.

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
| `snapshot_url` | yes | That machine's camera snapshot endpoint. |
| `calibration_path` | yes | That machine's calibration file. |
| `id` | no | Topic and unique-ID prefix. Defaults to `type`; set it only to run two machines of the same type, and never change it afterwards — it is the entity identity. |
| `name` | no | Device name in Home Assistant. Defaults to e.g. "Speed Queen DR7". Renaming later won't rename entities that already exist. |
| `poll_active` | no | Seconds between reads while the panel is lit (default 10). |
| `poll_idle` | no | Seconds between reads while it's dark (default 60). |

## What you get

One device per machine. For a DR7 configured with the default `id`:

- `sensor.speed_queen_dr7_display` — raw decode, including letter codes
- `sensor.speed_queen_dr7_time_remaining` — minutes, unavailable during a code
- `sensor.speed_queen_dr7_state` — off / ready / running / cooling / done / fault
- `sensor.*_cycle`, `*_temp`, `*_dryness` — collapsed, one lit LED each
- 13 binary sensors — status, options, alerts
- `binary_sensor.*_decode_problem` — anchors lost or an unknown segment pattern
- `camera.*_panel` — the annotated frame, for re-aiming

A TR7 is the same shape: `*_cycle`, `*_temp`, `*_load_size` and `*_soil`
collapsed, 11 binary sensors (Wash, Rinse, Spin, the six options, and the two
locks), and a state of off / ready / running / fault — no `cooling`, and no
`done`, as above.

## Notes

- Polling drops to 60s when a panel is dark, 10s when anything is lit, per
  machine. This is mostly about keeping the XIAO cool.
- `decode_problem` is the entity to alert on. Without it, a bumped camera
  silently freezes every other entity at its last value.
- A fault code on the display sets `state: fault` but does **not** set
  `decode_problem` — that's a correct read of a real condition.
- Each machine has its own availability topic alongside a shared one for the
  add-on. One camera dropping off marks only that machine unavailable.
- A missing calibration file takes only its own machine out; the others keep
  publishing, and it's picked up as soon as the file appears.
