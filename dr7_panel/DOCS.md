# Speed Queen DR7 panel reader

Reads the DR7 control panel with a camera and publishes about twenty entities
to Home Assistant. No machine learning: indicators are brightness thresholds,
digits are seven-segment lookups.

## 1. Flash the camera

Add `dryer-cam.yaml` to ESPHome. You'll need three secrets: `wifi_ssid`,
`wifi_password`, `dryer_cam_api_key`, `dryer_cam_ota_password`.

Mount before you calibrate — the ROI coordinates are tied to the exact camera
position. Aim roughly perpendicular to the tilted panel face, hood it against
room light, and check the reflection isn't sitting on the left third of the
panel where the Perm Press and Sensing LEDs live.

Then tune `aec_value` (start at 300). Open `http://dryer-cam.local:8081/` and
look at the digits: you want lit segments clearly bright but **not** clipped to
solid white, with a visible dark gap between neighbouring segments. Too high
and they bloom together; too low and you'll catch the display mid-refresh with
segments missing. Nudge in steps of 50.

## 2. Calibrate

Capture at least four snapshots covering different machine states — idle,
mid-cycle, cooling, complete. Every indicator needs to appear both lit and
unlit across the set, and every segment needs to appear both ways too (running
through 0-9 on the time display covers all seven).

Open `dr7-calibrate.html` in a browser, load all the snapshots, then:

1. **Anchors** — click the centre of the ▲ and the ▼ next to the display.
2. **Indicators** — click each of the 31 LED centres in the order prompted.
3. **Digits** — box each digit tightly around its outer segments.

Circles and boxes turn cyan when the tool reads them as lit, amber when dark.
Flip between frames to confirm they follow the real state. Then export.

If `needs_more_frames` comes back non-empty, those ROIs never changed state
across your snapshots and got a fallback threshold. Capture more frames and
re-export rather than shipping a guess.

Save the result to `/config/dr7_panel/calibration.json`.

## 3. Install the add-on

Copy the `addon/` contents to `/addons/dr7_panel/` on the HA host (Samba or
the SSH add-on). Reload the add-on store, and it appears under Local add-ons.

Requires the Mosquitto broker add-on — credentials come from the Supervisor
MQTT service, so there's nothing to configure by hand.

## What you get

One "Speed Queen DR7" device with:

- `sensor.speed_queen_dr7_display` — raw decode, including letter codes
- `sensor.speed_queen_dr7_time_remaining` — minutes, unavailable during a code
- `sensor.speed_queen_dr7_state` — off / ready / running / cooling / done / fault
- `sensor.*_cycle`, `*_temp`, `*_dryness` — collapsed, one lit LED each
- 13 binary sensors — status, options, alerts
- `binary_sensor.*_decode_problem` — anchors lost or an unknown segment pattern
- `camera.*_panel` — the annotated frame, for re-aiming

## Notes

- Polling drops to 60s when the panel is dark, 10s when anything is lit. This
  is mostly about keeping the XIAO cool.
- `decode_problem` is the entity to alert on. Without it, a bumped camera
  silently freezes every other entity at its last value.
- A fault code on the display sets `state: fault` but does **not** set
  `decode_problem` — that's a correct read of a real condition.
