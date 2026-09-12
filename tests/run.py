#!/usr/bin/env python3
"""Run every check. Exits non-zero if any fail.

    python3 tests/run.py

Needs the add-on's own dependencies — numpy, Pillow and paho-mqtt — because
it imports decoder.py directly. Nothing else.
"""

import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import test_decode                                  # noqa: E402
import test_profiles                                # noqa: E402
import test_server                                  # noqa: E402
from harness import Suite                           # noqa: E402

SUITES = (
    ("profiles", "Checking the calibration tool against the decoder's profiles",
     test_profiles.run, ""),
    ("decode", "Decoding synthetic frames", test_decode.run,
     "the decoder warnings below are produced on purpose, by the checks for a\n"
     "  wrong-channel file, an unreachable camera and a missing calibration file"),
    ("server", "Serving the calibration UI", test_server.run,
     "the rejected uploads below are the checks for what the save endpoint\n"
     "  refuses to write"),
)


def main():
    suites = []
    for name, title, fn, note in SUITES:
        print(f"\n{title}...")
        if note:
            print(f"  ({note})")
        suite = Suite(name)
        suites.append(suite)
        try:
            fn(suite)
        except Exception as exc:
            # A broken invariant can surface as an exception rather than a
            # failed comparison — a topic that is never published, say. The
            # suite is passed in rather than returned so the checks that had
            # already run are still counted, and later suites still run.
            print()
            traceback.print_exc()
            suite.failures.append(f"stopped early: {exc!r}")
        # Report each suite as it finishes, so a later crash cannot swallow
        # what an earlier one already found.
        suite.report()

    total = sum(s.checks for s in suites)
    failed = sum(len(s.failures) for s in suites)
    print(f"\n{total} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
