# XIAO ESP32S3 Sense → Speed Queen DR7

Reads a Speed Queen DR7 dryer's control panel with a Seeed Studio XIAO ESP32S3
Sense and publishes it to Home Assistant — the two-digit display plus all 31
indicator lights, as about twenty entities.

No machine learning. Indicators are brightness thresholds over a small disc;
digits are seven fixed sub-regions per character, thresholded and looked up in
a table. That table includes the letter forms, so diagnostic codes like `nH`,
`AF` and `dL` decode as readily as `46` does.

## Install

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fambient-home-systems%2FXIAO-ESP32S3-Sense-Speedqueen)

That opens the repository dialog with the URL filled in. It goes through
[My Home Assistant](https://my.home-assistant.io/), which needs your instance's
URL configured once under **Settings → System → Network → My Home Assistant**.

Otherwise, add it by hand — **Settings → Add-ons → Add-on Store → ⋮ →
Repositories**, then:

```
https://github.com/ambient-home-systems/XIAO-ESP32S3-Sense-Speedqueen
```

Either way, "Speed Queen DR7 panel reader" then appears under this repository.
It builds locally on first install, which takes a few minutes.

Requires the Mosquitto broker add-on. Credentials come from the Supervisor MQTT
service, so there is nothing to configure by hand.

## What's here

| Path | What it is |
|---|---|
| `dr7_panel/` | The add-on. Setup details are in its documentation tab. |
| `esphome/dryer-cam.yaml` | Camera firmware, with every auto-exposure feature deliberately off. |
| `tools/dr7-calibrate.html` | Browser tool that produces `calibration.json`. Open it locally; nothing is uploaded. |

## Order of operations

Mount the camera, then tune exposure, then calibrate, then install the add-on.
The ROI coordinates are tied to the camera's exact position, so calibrating
before the mount is final means doing it twice.

Full instructions: [`dr7_panel/DOCS.md`](dr7_panel/DOCS.md).

## Hardware notes

The XIAO ESP32S3 Sense runs hot enough to crash intermittently under sustained
load. Fit a small heatsink. The ESPHome config keeps the idle frame rate low
partly for this reason.

The camera pin mapping in `dryer-cam.yaml` is the widely-used community
mapping rather than an official ESPHome board profile. Newer Sense boards ship
an OV3660 in place of the discontinued OV2640 and need a recent ESPHome.

## Licence

MIT.
