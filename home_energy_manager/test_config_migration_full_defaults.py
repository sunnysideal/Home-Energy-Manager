from copy import deepcopy
from pathlib import Path

import yaml

from config_migration import migrate_runtime_options

ROOT = Path(__file__).resolve().parent


def test_shipped_legacy_options_render_identical_private_component_contracts():
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    saved = deepcopy(config["options"])
    before = deepcopy(saved)

    runtime, canonical, diagnostics = migrate_runtime_options(saved)

    assert saved == before, "in-memory migration must not rewrite saved options"
    assert diagnostics.source_layout == "legacy"
    assert set(canonical) == {
        "battery",
        "grid",
        "inverter",
        "tariff",
        "solar",
        "heat_pump",
        "dhw",
        "ev",
        "energy_strategy",
        "advanced",
    }

    # Stage-one migration is behaviour preserving: the current public 0.1.37
    # component-oriented configuration must render back to exactly the same
    # private contracts consumed by each process.
    assert runtime["ashp_forecaster"] == before["ashp_forecaster"]
    assert runtime["home_forecaster"] == before["home_forecaster"]
    assert runtime["controller"] == before["controller"]

    # Shared concepts now have a single canonical owner even though the public
    # Home Assistant UI is intentionally unchanged during this safe stage.
    assert canonical["inverter"]["house_load_energy_total_kwh"] == before["home_forecaster"]["load"]["energy_total_kwh"]
    assert canonical["heat_pump"]["ch_energy_total_kwh"] == before["ashp_forecaster"]["ch_energy_entity"]
    assert canonical["dhw"]["energy_total_kwh"] == before["ashp_forecaster"]["dhw_energy_entity"]
    assert canonical["advanced"]["internal"]["ashp_forecast_entity"] == before["home_forecaster"]["ashp"]["forecast_48h"]
