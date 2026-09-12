# Speed Queen panel reader

Reads a Speed Queen 7-series control panel with a camera and publishes about
twenty entities per machine to Home Assistant. No machine learning: indicators
are brightness thresholds, digits are seven-segment lookups.

One add-on instance handles any number of machines. Each needs its own camera
node and its own calibration file.

## Machines

| `type` | Machine | Status |
|---|---|---|
| `dr7` | DR7 dryer | Verified against photographs of a real panel |
| `tr7` | TR7 washer | **Provisional** — legend never checked against hardware |

The `tr7` profile's indicator names and labels are a best guess at the printed
legend. The decoding machinery is the same code the DR7 uses, so what needs
checking is only the *list*: open `tools/sq-calibrate.html`, pick **TR7
washer**, and compare the prompts against your panel. Anything wrong is fixed
in the `MACHINES` table at the top of that file's script — the add-on takes
indicator labels from the calibration file you export, so a renamed legend
needs no add-on change. Adding or removing an indicator, or moving one between
groups, also needs the matching entry in `PROFILES` in `decoder.py`.

## 1. Flash the cameras

Put `panel-cam-base.yaml` and the per-node file (`dryer-cam.yaml`,
`washer-cam.yaml`) in your ESPHome config directory — the per-node files
include the shared one. Secrets: `wifi_ssid`, `wifi_password`, plus
`dryer_cam_api_key` / `dryer_cam_ota_password` and the same pair prefixed
`washer_cam_` for the second node.

Mount before you calibrate — the ROI coordinates are tied to the exact camera
position. Aim roughly perpendicular to the tilted panel face, hood it against
room light, and check the reflection isn't sitting on the left third of the
panel where the Perm Press and Sensing LEDs live.

Then tune `aec_value` (start at 300) in that node's file. Open
`http://dryer-cam.local:8081/` and look at the digits: you want lit segments
clearly bright but **not** clipped to solid white, with a visible dark gap
between neighbouring segments. Too high and they bloom together; too low and
you'll catch the display mid-refresh with segments missing. Nudge in steps of
50. Each panel sits in its own light, so the two nodes will usually end up on
different values.

## 2. Calibrate, once per machine

Capture at least four snapshots covering different machine states — idle,
mid-cycle, and whatever the end of a cycle looks like. Every indicator needs to
appear both lit and unlit across the set, and every segment needs to appear
both ways too (running through 0-9 on the time display covers all seven).

Open `sq-calibrate.html` in a browser, choose the machine, load all the
snapshots, then:

1. **Anchors** — click the centre of the ▲ and the ▼ next to the display.
2. **Indicators** — click each LED centre in the order prompted.
3. **Digits** — box each digit tightly around its outer segments.

Circles and boxes turn cyan when the tool reads them as lit, amber when dark.
Flip between frames to confirm they follow the real state. Then export.

If `needs_more_frames` comes back non-empty, those ROIs never changed state
across your snapshots and got a fallback threshold. Capture more frames and
re-export rather than shipping a guess.

Save each machine's file somewhere under `/config`, one file per machine —
`/config/speedqueen_panel/dr7.json` and `/config/speedqueen_panel/tr7.json`
match the defaults. The exported file records which machine it was built for,
and the add-on refuses to start if it doesn't match the configured `type`.

## 3. Install the add-on

Copy the `speedqueen_panel/` contents to `/addons/speedqueen_panel/` on the HA
host (Samba or the SSH add-on). Reload the add-on store, and it appears under
Local add-ons.

Requires the Mosquitto broker add-on — credentials come from the Supervisor
MQTT service, so there's nothing to configure by hand.

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

A TR7 is the same shape, with `*_cycle`, `*_soil`, `*_temp` and `*_spin_speed`
collapsed, and `running` in place of `cooling` in the state list.

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
