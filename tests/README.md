# Checks

Run them by hand before pushing a change to `decoder.py` or to the calibration
tool's `MACHINES` table:

```
python3 tests/run.py
```

Exits non-zero if anything fails. There is no CI in this repository by
design, so nothing runs these for you.

They need the add-on's own dependencies and nothing else — numpy, Pillow and
paho-mqtt, because `decoder.py` imports paho at module level:

```
pip3 install numpy pillow paho-mqtt
```

## What's here

| File | What it does |
|---|---|
| `run.py` | Runs everything, one exit code. |
| `test_profiles.py` | Static: the calibration tool and `PROFILES` must agree. |
| `test_decode.py` | Decodes synthetic frames and inspects the MQTT payloads. |
| `test_server.py` | Drives the ingress UI over real HTTP: the page, the snapshot proxy, and everything the save endpoint refuses to write. |
| `synth.py` | Draws the frames and builds the calibration file a tool export would produce. |
| `toollists.py` | Reads the `MACHINES` table out of the tool's HTML. |
| `harness.py` | Check collection and a stand-in MQTT client. |

## Why these two suites

**`test_profiles.py` catches drift.** The calibration tool owns the indicator
legend; `decoder.py` owns what happens to it. They are different files in
different languages, and nothing else stops them disagreeing — a renamed
indicator, a group that collapses on one side only, a state rule naming a
light that no longer exists, the two disagreeing about which colour channel to
sample. Each of those would otherwise surface as a missing or permanently-off
entity, long after the change that caused it.

**`test_server.py` guards the one endpoint that writes to disk.** The save
endpoint puts a file in the Home Assistant config directory, so most of what
it checks is what gets refused: a calibration for the wrong machine, a bad
version, malformed JSON, an unknown machine id, and an id shaped like a path
traversal. The destination always comes from the add-on's configuration rather
than the request, and a check confirms no refused upload changed the file on
disk.

**`test_decode.py` catches regressions in the decode path**, and pins two
things that are expensive to get wrong:

- **The DR7's MQTT identity.** Its topics and unique IDs must stay exactly
  what 0.1.x published (`dr7/panel/…`, `speedqueen_dr7_…`) or every existing
  entity in someone's Home Assistant is orphaned. Those strings are asserted
  literally.
- **The per-machine sampling channel.** The synthetic frames carry the colours
  measured off photographs of the real panels, so a TR7 frame reproduces the
  inversion that makes the channel matter: sampling red reads its lit Wash
  light as off and its unlit lights as on. If someone ever "simplifies" the
  channel back to one shared setting, that check fails.

## What they don't do

The frames are flat discs and rectangles, not photographs. They exercise the
decode path's arithmetic — thresholds, the anchor transform, the glyph table,
the state rules, the payload shape — and say nothing about optics, exposure,
glare, or whether a real panel looks like the numbers here. Only a mounted
camera answers those.

## Adding a machine

1. Add its list to `MACHINES` in `speedqueen_panel/sq-calibrate.html`.
2. Add the matching profile to `PROFILES` in `speedqueen_panel/decoder.py`.
3. Add a palette entry to `synth.py` with that panel's measured lit and unlit
   levels.
4. Run `python3 tests/run.py`. `test_profiles.py` picks the new machine up on
   its own and will tell you what disagrees.
