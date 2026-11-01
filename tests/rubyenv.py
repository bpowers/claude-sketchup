"""The Ruby the Ruby tests run: the first `ruby` on the PATH, when it is 3.2 or later (SketchUp embeds Ruby 3.2, so
build.rb and the watcher are written for it). An older one, such as the Ruby 2.6 macOS ships in /usr/bin, is not used,
and the Ruby tests skip saying why (README.md, "What you need", puts a newer one on the PATH)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

MINIMUM = (3, 2)


def _find() -> tuple[str | None, str]:
    """(the ruby to run, or None; the reason the Ruby tests skip when None)."""
    ruby = shutil.which("ruby")
    if ruby is None:
        return None, "no ruby on the PATH (README.md: brew install ruby, then put it first on the PATH)"
    try:
        version = subprocess.run([ruby, "-e", "print RUBY_VERSION"], capture_output=True, text=True,
                                 timeout=30).stdout.strip()
        have = tuple(int(p) for p in version.split(".")[:2])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, f"{ruby} did not say its version"
    if have < MINIMUM:
        return None, (f"{ruby} is Ruby {version}, older than {'.'.join(map(str, MINIMUM))} (SketchUp's); put a newer "
                      "one first on the PATH (README.md)")
    return ruby, ""


RUBY, WHY = _find()
needs_ruby = pytest.mark.skipif(RUBY is None, reason=WHY)
