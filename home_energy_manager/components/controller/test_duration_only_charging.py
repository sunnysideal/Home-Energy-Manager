from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.parents[1]
AGENTS = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
ACTIVE = (ROOT / 'active_runtime.py').read_text(encoding='utf-8')
AXLE = (ROOT / 'axle_only_runtime.py').read_text(encoding='utf-8')
DOCKER = (PACKAGE / 'Dockerfile').read_text(encoding='utf-8')


def test_charge_target_is_user_owned_contract():
    assert 'Charge target is invariant' in AGENTS
    assert 'must never write or alter the inverter charge-target SOC entity' in AGENTS
    assert 'Charge quantity must be controlled by charge-slot duration and charge rate only' in AGENTS


def test_active_controller_suppresses_charge_target_writes():
    assert "if field == 'charge_target':" in ACTIVE
    assert 'return True' in ACTIVE
    assert "core.Controller.ensure = _ensure_without_charge_target" in ACTIVE


def test_axle_only_suppresses_discovered_charge_target_writes():
    assert "ents.get('charge_slot_1_target')" in AXLE
    assert "entity_id == getattr(self, '_charge_target_entity', '')" in AXLE
    assert 'return' in AXLE


def test_runtime_image_uses_protected_entrypoints():
    assert 'COPY components/controller/minimise_export_runtime.py /app/runtime/controller/minimise_export_runtime.py' in DOCKER
    assert 'COPY components/controller/active_runtime.py /app/runtime/controller/app.py' in DOCKER
    assert 'COPY components/controller/axle_only.py /app/runtime/controller/axle_only_core.py' in DOCKER
    assert 'COPY components/controller/axle_only_runtime.py /app/runtime/controller/axle_only.py' in DOCKER
