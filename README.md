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

Either way, "Speed Queen panel reader" then appears under this repository. It
builds locally on first install, which takes a few minutes.

Requires the Mosquitto broker add-on. Credentials come from the Supervisor MQTT
service, so there is nothing to configure by hand.

> Upgrading from 0.1.x, which was called "Speed Queen DR7 panel reader"?
> Supervisor sees the new name as a separate add-on, so it won't offer an
> update — uninstall the old one and install this. Your entities and your
> existing `calibration.json` carry over untouched. See the
> [changelog](speedqueen_panel/CHANGELOG.md) for the steps.

## What's here

| Path | What it is |
|---|---|
| `speedqueen_panel/` | The add-on. Setup details are in its documentation tab. |
| `esphome/panel-cam-base.yaml` | The camera block both nodes share, with every auto-exposure feature deliberately off. |
| `esphome/dryer-cam.yaml`, `washer-cam.yaml` | Per-node files: name, secrets, exposure. |
| `tools/sq-calibrate.html` | Browser tool that produces `calibration.json`. Open it locally; nothing is uploaded. |

## Order of operations

Mount the camera, then tune exposure, then calibrate, then install the add-on.
The ROI coordinates are tied to the camera's exact position, so calibrating
before the mount is final means doing it twice. Repeat the first three for the
second machine; the add-on is installed once either way.

Full instructions: [`speedqueen_panel/DOCS.md`](speedqueen_panel/DOCS.md).

## Machine support

The DR7 profile was built from photographs of a real panel. **The TR7 profile
has never been checked against hardware** — its indicator legend is a best
guess, and the calibration tool is where you correct it. The decode path is
shared, so nothing but the list of indicators is in question.

## Hardware notes

The XIAO ESP32S3 Sense runs hot enough to crash intermittently under sustained
load. Fit a small heatsink. The ESPHome config keeps the idle frame rate low
partly for this reason.

The camera pin mapping in `panel-cam-base.yaml` is the widely-used community
mapping rather than an official ESPHome board profile. Newer Sense boards ship
an OV3660 in place of the discontinued OV2640 and need a recent ESPHome.

## Licence

MIT.
