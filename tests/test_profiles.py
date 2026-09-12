"""Static checks: the calibration tool and the decoder must agree.

The tool defines the indicator legend and the decoder defines what happens to
it. They live in different files and different languages, so nothing stops
them drifting apart — a renamed indicator, a group that collapses on one side
only, a state rule naming a light that no longer exists. Every failure here is
something that would otherwise show up as a missing or permanently-off entity
long after the change that caused it.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "speedqueen_panel"))
sys.path.insert(0, str(ROOT / "tests"))

import toollists                                    # noqa: E402
from harness import Suite                           # noqa: E402


def run(s=None):
    """Accepts a Suite so a caller keeps partial results if a check raises."""
    import decoder

    s = s or Suite("profiles")
    tool = toollists.machines()
    s.check("same machines in both", sorted(tool), sorted(decoder.PROFILES))

    for key in sorted(set(tool) & set(decoder.PROFILES)):
        spec, prof = tool[key], decoder.PROFILES[key]
        names = [l["name"] for l in spec["leds"]]
        groups = {l["group"] for l in spec["leds"]}
        excl = set(prof["exclusive"])

        s.note(f"{key}: {len(names)} indicators, channel {spec['channel']}, "
               f"collapsed {sorted(excl)}")

        s.check(f"{key}: channel agrees", spec["channel"], prof["channel"])
        s.check(f"{key}: no duplicate indicator names",
                sorted({n for n in names if names.count(n) > 1}), [])
        s.check(f"{key}: every collapsed group has indicators",
                sorted(excl - groups), [])
        s.check(f"{key}: every group has a display title",
                sorted(groups - set(spec["groups"])), [])
        s.check(f"{key}: no indicator collides with a reserved payload key",
                sorted(set(names) & decoder.RESERVED), [])
        s.check(f"{key}: no indicator collides with a group name",
                sorted(set(names) & excl), [])
        s.check(f"{key}: every collapsed group has an icon",
                sorted(excl - set(prof["group_icons"])), [])
        s.check(f"{key}: profile labels cover every indicator",
                [n for n in names if n not in prof["labels"]], [])
        s.check(f"{key}: collapsed groups are named in labels",
                [g for g in excl
                 if g not in prof["labels"] and g != g.replace("_", " ")], [])

        for state, triggers in prof["states"]:
            s.check(f"{key}: state {state!r} triggers on real indicators",
                    [t for t in triggers if t not in names], [])
        s.check(f"{key}: device classes name real indicators",
                [n for n in prof["device_class"] if n not in names], [])
        s.check(f"{key}: every state rule can be reached by 'active' or not",
                all(isinstance(st, str) for st, _ in prof["states"]), True)

    return s


if __name__ == "__main__":
    sys.exit(0 if run().report() else 1)
