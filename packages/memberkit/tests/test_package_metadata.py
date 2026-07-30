import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def _metadata():
    path = Path(__file__).parents[1] / "pyproject.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_package_metadata_declares_windows_tzdata_without_hub_dependency():
    project = _metadata()["project"]

    assert "Operating System :: Microsoft :: Windows" in project["classifiers"]
    assert project["dependencies"] == ["tzdata; sys_platform == 'win32'"]
    names = [dependency.partition(";")[0].strip() for dependency in project["dependencies"]]
    assert all(name.lower().replace("_", "-").replace(".", "-") != "teammem" for name in names)


@pytest.mark.skipif(sys.platform != "win32", reason="tzdata fallback is Windows-only")
def test_windows_tzdata_resolves_daylight_saving_without_system_zoneinfo():
    code = """
from datetime import datetime
import zoneinfo

zoneinfo.reset_tzpath(())
zone = zoneinfo.ZoneInfo("America/Los_Angeles")
assert datetime(2026, 3, 1, tzinfo=zone).utcoffset() != datetime(2026, 3, 15, tzinfo=zone).utcoffset()
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
