from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
TRANSLATION = ROOT / "translations" / "en.yaml"


def _schema_tree(node):
    if not isinstance(node, dict):
        return None
    return {key: _schema_tree(value) for key, value in node.items()}


def _assert_translation_node(schema_node, translation_node, path):
    assert isinstance(translation_node, dict), f"Missing translation node for {path}"
    assert str(translation_node.get("name") or "").strip(), f"Missing name for {path}"
    assert str(translation_node.get("description") or "").strip(), f"Missing description for {path}"

    if schema_node is None:
        assert "fields" not in translation_node, f"Unexpected nested fields for leaf {path}"
        return

    fields = translation_node.get("fields")
    assert isinstance(fields, dict), f"Missing fields mapping for {path}"
    assert set(fields) == set(schema_node), (
        f"Translation/schema key mismatch at {path}: "
        f"missing={sorted(set(schema_node) - set(fields))}, "
        f"extra={sorted(set(fields) - set(schema_node))}"
    )
    for key, child in schema_node.items():
        _assert_translation_node(child, fields[key], f"{path}.{key}")


def test_config_translation_covers_current_nested_schema():
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    translation = yaml.safe_load(TRANSLATION.read_text(encoding="utf-8"))

    schema = _schema_tree(config["schema"])
    translated = translation.get("configuration")
    assert isinstance(translated, dict)
    assert set(translated) == set(schema), (
        f"Top-level translation/schema mismatch: "
        f"missing={sorted(set(schema) - set(translated))}, "
        f"extra={sorted(set(translated) - set(schema))}"
    )

    for key, child in schema.items():
        _assert_translation_node(child, translated[key], key)


def test_old_component_translation_namespaces_are_gone():
    translation = yaml.safe_load(TRANSLATION.read_text(encoding="utf-8"))["configuration"]
    for old_key in ("ashp_forecaster", "home_forecaster", "controller", "axle_adapter"):
        assert old_key not in translation
