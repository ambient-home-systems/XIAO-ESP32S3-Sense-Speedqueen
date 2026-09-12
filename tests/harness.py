"""Shared scaffolding for the panel decoder checks.

Deliberately not pytest: the add-on's runtime dependencies are numpy, Pillow
and paho-mqtt, and this repository runs no CI, so the checks have to be
runnable by hand with nothing else installed.
"""

import json


class Suite:
    """Collects check results so one run reports every failure, not the first."""

    def __init__(self, name):
        self.name = name
        self.checks = 0
        self.failures = []

    def check(self, label, got, want):
        self.checks += 1
        if got != want:
            self.failures.append(f"{label}: got {got!r}, want {want!r}")
        return got == want

    def truthy(self, label, got):
        return self.check(label, bool(got), True)

    def expect_error(self, label, fn, needle):
        """Assert fn raises, and that the message names the real reason."""
        self.checks += 1
        try:
            fn()
        except Exception as exc:
            if needle.lower() not in str(exc).lower():
                self.failures.append(
                    f"{label}: raised {exc!r}, expected a message mentioning {needle!r}")
        else:
            self.failures.append(f"{label}: no error raised")

    def note(self, line):
        print(f"  {line}")

    def report(self):
        if self.failures:
            print(f"\n{self.name}: {len(self.failures)} of {self.checks} checks FAILED")
            for f in self.failures:
                print(f"  - {f}")
        else:
            print(f"\n{self.name}: {self.checks} checks passed")
        return not self.failures


class FakeClient:
    """Stands in for paho's client and records what would have been published."""

    def __init__(self):
        self.pubs = []

    def publish(self, topic, payload=None, retain=False):
        self.pubs.append((topic, payload, retain))

    def topics(self):
        return [t for t, _, _ in self.pubs]

    def discovery_topics(self):
        return [t for t in self.topics() if t.startswith("homeassistant/")]

    def last_json(self, topic):
        for t, p, _ in reversed(self.pubs):
            if t == topic:
                return json.loads(p)
        raise AssertionError(f"nothing was published to {topic}")
