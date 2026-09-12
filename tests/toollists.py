"""Read the calibration tool's MACHINES table out of the HTML.

The tool is a single dependency-free file, so its indicator lists live in a
JavaScript literal. Rather than add a JS runtime to run the checks, this
parses the literal directly. It is deliberately strict — a parse that finds
nothing raises rather than quietly reporting an empty table, because a silent
empty result would make the cross-check pass for the wrong reason.
"""

import pathlib
import re

TOOL = (pathlib.Path(__file__).resolve().parent.parent
        / "speedqueen_panel" / "sq-calibrate.html")

_BLOCK = re.compile(r"const MACHINES = \{(.*?)\n\};", re.S)
_KEY = re.compile(r"^  (\w+): \{$", re.M)
_LED = re.compile(r'\["([^"]+)",\s*"([^"]+)",\s*"((?:[^"\\]|\\.)*)"\]')
_GROUP = re.compile(r'(\w+):\s*"((?:[^"\\]|\\.)*)"')


def _field(body, name, pattern, machine, required=True):
    m = re.search(rf"{name}:\s*{pattern}", body)
    if m:
        return m.group(1)
    if required:
        raise ValueError(f"{machine}: no {name} in the tool's MACHINES table")
    return None


def machines(path=TOOL):
    src = path.read_text(encoding="utf-8")
    block = _BLOCK.search(src)
    if not block:
        raise ValueError(f"no MACHINES table found in {path}")
    block = block.group(1)

    keys = [(m.group(1), m.start()) for m in _KEY.finditer(block)]
    if not keys:
        raise ValueError(f"no machines found in the MACHINES table in {path}")

    out = {}
    for i, (key, start) in enumerate(keys):
        end = keys[i + 1][1] if i + 1 < len(keys) else len(block)
        body = block[start:end]

        groups_src = re.search(r"groups:\s*\{(.*?)\}", body, re.S)
        if not groups_src:
            raise ValueError(f"{key}: no groups in the tool's MACHINES table")
        leds_src = re.search(r"leds:\s*\[(.*?)\n    \]", body, re.S)
        if not leds_src:
            raise ValueError(f"{key}: no leds in the tool's MACHINES table")

        leds = _LED.findall(leds_src.group(1))
        if not leds:
            raise ValueError(f"{key}: leds list parsed as empty")

        out[key] = {
            "digits": int(_field(body, "digits", r"(\d+)", key)),
            "channel": _field(body, "channel", r'"(\w)"', key),
            "note": _field(body, "note", r'"([^"]*)"', key, required=False),
            "groups": dict(_GROUP.findall(groups_src.group(1))),
            "leds": [{"group": g, "name": n, "label": lab} for g, n, lab in leds],
        }
    return out


if __name__ == "__main__":
    for name, spec in machines().items():
        print(f"{name}: {len(spec['leds'])} indicators, channel {spec['channel']}, "
              f"{spec['digits']} digits, groups {sorted(spec['groups'])}")
