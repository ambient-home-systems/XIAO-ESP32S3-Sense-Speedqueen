# Changelog

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
