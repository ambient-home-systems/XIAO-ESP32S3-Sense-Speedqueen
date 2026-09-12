# XIAO ESP32S3 Sense → Speed Queen panel reader

Reads a Speed Queen 7-series control panel with a Seeed Studio XIAO ESP32S3
Sense and publishes it to Home Assistant — the two-digit display plus every
indicator light, as about twenty entities per machine.

Two machines are supported: the **DR7** dryer and the **TR7** washer. One
add-on instance drives both (or several of either), each with its own camera
node and calibration file.

No machine learning. Indicators are brightness thresholds over a small disc;
digits are seven fixed sub-regions per character, thresholded and looked up in
a table. That table includes the letter forms, so diagnostic codes like `nH`,
`AF` and `dL` decode as readily as `46` does.

## Add the add-on repository

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fambient-home-systems%2FXIAO-ESP32S3-Sense-Speedqueen)

That opens the repository dialog with the URL filled in. It goes through
[My Home Assistant](https://my.home-assistant.io/), which needs your instance's
URL configured once under **Settings → System → Network → My Home Assistant**.

Otherwise add it by hand — **Settings → Add-ons → Add-on Store → ⋮ →
Repositories** — with this URL:

```
https://github.com/ambient-home-systems/XIAO-ESP32S3-Sense-Speedqueen
```

> Upgrading from 0.1.x, which was called "Speed Queen DR7 panel reader"?
> Supervisor sees the new name as a separate add-on, so it won't offer an
> update — uninstall the old one and install this. Your entities and your
> existing `calibration.json` carry over untouched. See the
> [changelog](speedqueen_panel/CHANGELOG.md) for the steps.

---

# Setting it up

Steps 1 to 5 are done once per machine. Step 6 is done once, however many
machines you have. Budget an hour for the first machine; most of that is
mounting and calibrating, and the second machine goes much faster.

**Do them in order.** The calibration in step 5 records pixel coordinates, so
it is only valid for the camera position fixed in step 2 and the exposure fixed
in step 3. Calibrating before the mount is final means calibrating twice.

## What you need

Per machine:

- A Seeed Studio **XIAO ESP32S3 Sense** — the variant with the camera daughter
  board, not the plain XIAO ESP32S3
- **A small heatsink.** The board runs hot enough to crash intermittently under
  sustained load without one
- A USB-C cable, for the first flash only — everything after that is over the air
- A mount, and something to hood the camera against room light

In Home Assistant:

- The **ESPHome Device Builder** add-on (listed as just "ESPHome" on older
  installs)
- The **Mosquitto broker** add-on, and the MQTT integration set up. The panel
  reader takes its credentials from the Supervisor MQTT service, so there is
  nothing to type in by hand

## Step 1 — Flash the camera node

Copy **one** file into your ESPHome configuration directory: `washer-cam.yaml`
for a TR7, `dryer-cam.yaml` for a DR7. It pulls the shared camera block from
this repository, so there is no second file to copy or keep in sync.

Add that node's secrets to ESPHome's `secrets.yaml`. Generate the API key with
`openssl rand -base64 32`, or copy the one ESPHome offers when you create a new
device — it has to be 32 bytes of base64, not a passphrase:

```yaml
washer_cam_api_key: "PASTE-32-BYTES-OF-BASE64-HERE="
washer_cam_ota_password: "anything-you-like"
wifi_ssid: "YourNetwork"
wifi_password: "YourPassword"
```

Prefer not to put Wi-Fi credentials in the firmware? Delete the `wifi:` block
from the node file and skip the last two. The node will come up as its own open
hotspot named `washer-cam`, serving a page that asks which network to join;
what you enter is saved to the board and survives later updates.

Now get the firmware onto the board. **Only this first flash needs a cable.**

- **If the board is plugged into your Home Assistant host**, ESPHome installs
  it directly — **Install → Plug into this computer**.
- **Otherwise** (the usual case), in ESPHome choose **Install → Manual
  download**, pick the **Factory format** `.bin`, then open
  [web.esphome.io](https://web.esphome.io) in Chrome or Edge on whatever machine
  you downloaded it to, plug the board into *that* machine, and install the
  file. No drivers, nothing to install — the XIAO ESP32S3 has native USB.

If no serial port appears in the browser's picker, hold the **BOOT** button
while plugging the board in. That forces it into download mode.

> **Check before moving on:** the node shows as online in ESPHome, and
> `http://washer-cam.local:8081/` returns a JPEG in your browser. If the
> `.local` name doesn't resolve, use the node's IP address — and use the IP in
> step 6 too.

## Step 2 — Mount and aim the camera

Fix the camera in its final position now. Everything after this depends on it
not moving.

- Aim roughly **perpendicular to the panel face**, which is tilted on these
  machines — square to the glass, not square to the floor.
- **Hood it against room light.** Reflections are the main enemy.
- Check no reflection sits on the **left third** of the panel, where the Perm
  Press and Sensing indicators live on a DR7.
- Get the **whole panel** in frame, including both ▲▼ triangles beside the
  display. The decoder uses those triangles to correct for the camera being
  nudged later, so losing them costs you that protection.

> **Check before moving on:** a snapshot shows the entire panel, both
> triangles, and no glare across any indicator.

## Step 3 — Tune the exposure

Every auto-adjusting feature is deliberately off, so exposure is a fixed number
you set once. Edit `aec_value` in that node's file — start at `300` — install,
and look at a snapshot.

You want lit segments **clearly bright but not clipped to solid white**, with a
visible dark gap between neighbouring segments.

- Too high: segments bloom into each other.
- Too low: you catch the display mid-refresh with segments missing.

Nudge by 50 and reinstall (over the air now) until it looks right. Each panel
sits in its own light, so two nodes usually land on different values.

> **Check before moving on:** several refreshes of
> `http://washer-cam.local:8081/` all show every lit segment, with gaps between
> them, and none pure white.

## Step 4 — Capture calibration frames

Save **at least four** snapshots from that URL, covering different machine
states — idle, mid-cycle, and the end of a cycle.

Two things have to be true across the set, or calibration will guess:

- **Every indicator appears both lit and unlit** in at least one frame each.
- **Every digit segment appears both ways.** Running the time display through
  0–9 covers all seven.

## Step 5 — Calibrate

Open `tools/sq-calibrate.html` in a browser — download it from this repository
(right-click the raw file → **Save link as**) or clone the repo. It runs
entirely from `file://` and uploads nothing.

1. **Choose the machine first.** That sets the click list *and* the colour
   channel, which differs between the two machines and gets the decode
   backwards if it's wrong.
2. Load all your snapshots.
3. **Anchors** — click the centre of the ▲ and then the ▼ beside the display.
4. **Indicators** — click each indicator's centre, in the order prompted. The
   prompt names each one; work through them as it asks.
5. **Digits** — drag a box tightly around each digit's outer segments.

Circles and boxes turn **cyan** where the tool reads a lit ROI and **amber**
where it reads a dark one. Switch between frames and confirm they follow what
the panel was actually doing.

Then **Build calibration file** and save the JSON to your Home Assistant
config, one file per machine:

```
/config/speedqueen_panel/tr7.json
/config/speedqueen_panel/dr7.json
```

> **Check before moving on:** `needs_more_frames` in the exported file is
> empty. Anything listed there never changed state across your snapshots and
> got a fallback threshold — capture more frames and re-export rather than
> shipping a guess.

## Step 6 — Install and configure the add-on

Add this repository (the button at the top of this page), then install **Speed
Queen panel reader** from the store. It builds locally on first install, which
takes a few minutes.

In the add-on's **Configuration** tab, list your machines:

```yaml
machines:
  - type: tr7
    snapshot_url: "http://washer-cam.local:8081/"
    calibration_path: "/homeassistant/speedqueen_panel/tr7.json"
  - type: dr7
    snapshot_url: "http://dryer-cam.local:8081/"
    calibration_path: "/homeassistant/speedqueen_panel/dr7.json"
publish_debug_image: true
log_level: info
```

Note the path prefix: what is `/config` when you save the file is
`/homeassistant` from inside the add-on. Every option is documented in the
add-on's own [documentation tab](speedqueen_panel/DOCS.md).

Start it, and watch the **Log** tab.

> **Check:** the log names each machine and what it loaded, like
> `[tr7] TR7: 33 indicators, 2 digits, channel b`.

## Step 7 — Confirm it's reading the panel

A device per machine appears under **Settings → Devices & services → MQTT**.

1. Open `camera.*_panel` — the annotated frame. Every ROI is drawn on it, cyan
   for lit and amber for dark. This is the fastest way to see a misaimed camera
   or a bad threshold.
2. Compare `sensor.*_display` against the real panel.
3. Confirm `binary_sensor.*_decode_problem` is **off**.

Then **set up an alert on `decode_problem`.** It is the entity that exists
because silent failure is the real risk here: without it, a camera someone
knocks while moving a laundry basket freezes every other entity at its last
value, indefinitely, with nothing looking wrong.

## If something isn't right

| What you see | Likely cause |
|---|---|
| `decode_problem` on, entities frozen | Camera moved far enough that the ▲▼ anchors are lost, or an indicator has drifted off its ROI. Check `camera.*_panel`. |
| Every indicator reads backwards | Wrong colour channel — the calibration was built with the wrong machine selected. Re-export with the right one. The add-on logs a warning about this at startup. |
| Some indicators never light | Their threshold was guessed. Check `needs_more_frames` from step 5 and recapture. |
| Display decodes as `?` | A segment ROI is off, or exposure is blooming segments together. Retune step 3, then recalibrate. |
| A letter code like `nH` shows | That's a real fault code from the machine, decoded correctly. `state` goes to `fault`, and `decode_problem` stays off. |
| One machine unavailable, others fine | That camera is unreachable, or its calibration file is missing. Per-machine failures are isolated on purpose. |
| Nothing appears at all | Mosquitto isn't running, or the add-on stopped on a configuration error. Check the Log tab. |

---

## Machine support

| Machine | Indicators | Collapsed to one sensor each | Channel |
|---|---|---|---|
| DR7 dryer | 31 | Cycle, Temp, Dryness | red |
| TR7 washer | 33 | Cycle, Water Temp, Load Size, Soil Level | blue |

Both legends were read from photographs of the real panels; the calibration
tool is where you correct one if yours differs.

The sampling channel genuinely differs between them, which is why step 5 asks
you to pick the machine before anything else. The TR7 has no Complete light, so
its state goes `running` → `ready` at the end of a cycle rather than reporting
`done`. Both are explained in the
[add-on documentation](speedqueen_panel/DOCS.md).

## What's here

| Path | What it is |
|---|---|
| `speedqueen_panel/` | The add-on. Options, entities and machine details are in its documentation tab. |
| `esphome/panel-cam-base.yaml` | The camera block both nodes share, with every auto-exposure feature deliberately off. Nodes pull it straight from here, so it isn't copied around. |
| `esphome/dryer-cam.yaml`, `washer-cam.yaml` | Per-node files: name, secrets, exposure. The only file you need locally. |
| `tools/sq-calibrate.html` | Browser tool that produces `calibration.json`. Open it locally; nothing is uploaded. |
| `tests/` | Checks for the decoder and the tool's indicator lists. Run by hand: `python3 tests/run.py`. |

## Hardware notes

The XIAO ESP32S3 Sense runs hot enough to crash intermittently under sustained
load. Fit a small heatsink. The ESPHome config keeps the idle frame rate low
partly for this reason.

The camera pin mapping in `panel-cam-base.yaml` is the widely-used community
mapping rather than an official ESPHome board profile. Newer Sense boards ship
an OV3660 in place of the discontinued OV2640 and need a recent ESPHome.

## Licence

MIT.
