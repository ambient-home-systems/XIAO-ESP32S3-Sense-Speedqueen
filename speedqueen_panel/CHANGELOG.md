# Changelog

## 0.2.0

Supports any number of machines, and the DR7 is no longer baked into the
add-on's identity. The washer profile (TR7) is new and **has not been checked
against a real panel** — see "Machines" in the documentation.

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

- `tr7` machine profile for the washer: cycle, soil level, water temperature
  and spin speed collapse to one sensor each; fill/wash/rinse/spin/complete,
  options and alerts stay individual. **Provisional** — the indicator legend
  is a best guess and needs checking against the machine.
- The calibration tool has a machine picker, writes the machine type into
  `calibration.json`, and ships each indicator's label in the file. The
  add-on prefers those labels, so correcting a legend needs only the tool.
- Startup rejects a calibration file built for a different machine type, a
  duplicate or reserved indicator name, and two machines sharing an `id`.
- `esphome/panel-cam-base.yaml` holds the camera block both nodes share;
  `dryer-cam.yaml` and `washer-cam.yaml` are thin per-node files.

## 0.1.0

First release. Decodes the 31 panel indicators and the two-digit display,
publishes over MQTT discovery, and corrects for camera drift using the
printed ▲▼ triangles as anchors.
