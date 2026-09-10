from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def test_config_yaml_is_the_only_editable_package_version_source():
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")

    match = re.search(r"(?m)^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", config)
    assert match, "config.yaml must define the add-on version"
    version = match.group(1)

    assert "ARG BUILD_VERSION" in dockerfile
    assert 'ENV HOME_ENERGY_MANAGER_VERSION="${BUILD_VERSION}"' in dockerfile
    assert 'VERSION = os.environ.get("HOME_ENERGY_MANAGER_VERSION", "").strip()' in launcher
    assert "HOME_ENERGY_MANAGER_VERSION is missing" in launcher

    # No Python/Docker runtime file may duplicate the concrete package version.
    assert version not in dockerfile
    assert version not in launcher


def test_launcher_does_not_overwrite_build_supplied_version():
    launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
    assert 'os.environ["HOME_ENERGY_MANAGER_VERSION"] =' not in launcher
