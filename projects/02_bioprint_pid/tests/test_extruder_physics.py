"""FR-2: thermal model and power-law melt rheology."""

import numpy as np
import pytest
from extruder_physics import ExtruderThermalMass, gantry_speed_profile


@pytest.fixture
def plant() -> ExtruderThermalMass:
    return ExtruderThermalMass()


def test_calibrated_reference_pressure(plant) -> None:
    assert plant.nozzle_pressure(230.0, 1.0) == pytest.approx(45.0)


def test_colder_melt_needs_more_pressure(plant) -> None:
    """PRD verification: a 15 °C drop raises pressure (Arrhenius K(T))."""
    ratio = plant.nozzle_pressure(215.0, 1.0) / plant.nozzle_pressure(230.0, 1.0)
    assert 1.3 < ratio < 1.6


def test_shear_thinning(plant) -> None:
    """Doubling flow less than doubles pressure, because n < 1."""
    ratio = plant.nozzle_pressure(230.0, 1.0) / plant.nozzle_pressure(230.0, 0.5)
    assert ratio == pytest.approx(2**0.4)
    assert ratio < 2.0


def test_zero_flow_zero_pressure(plant) -> None:
    assert plant.nozzle_pressure(230.0, 0.0) == 0.0


def test_pressure_clamped(plant) -> None:
    assert plant.nozzle_pressure(-30.0, 1.0) == plant.rheology.max_pressure


def test_heater_raises_temperature(plant) -> None:
    t0 = plant.state.temperature
    plant.step(100.0, 1.0, 0.1)
    assert plant.state.temperature > t0


def test_cools_towards_ambient_without_heat(plant) -> None:
    plant.state.temperature = 200.0
    for _ in range(100):
        plant.step(0.0, 1.0, 0.1)
    assert 25.0 < plant.state.temperature < 200.0


def test_cold_extrusion_interlock(plant) -> None:
    plant.step(0.0, 1.0, 0.1)
    assert not plant.state.auger_enabled and plant.state.pressure == 0.0
    plant.state.temperature = 229.0
    plant.step(50.0, 1.0, 0.1)
    assert plant.state.auger_enabled and plant.state.pressure > 0.0


def test_feed_adds_cooling_load() -> None:
    """With the auger running, the same heater duty settles at a lower temperature."""

    def settle(flow: float) -> float:
        p = ExtruderThermalMass()
        p.state.temperature = 230.0
        p.state.auger_enabled = True
        for _ in range(3000):
            p.step(20.0, flow, 0.1)
        return p.state.temperature

    assert settle(1.0) < settle(0.0)


def test_disturbance_increases_losses(plant) -> None:
    plant.state.temperature = 230.0
    calm = ExtruderThermalMass()
    calm.state.temperature = 230.0
    plant.apply_ambient_disturbance(5.0, 30.0)
    plant.step(20.0, 0.0, 0.1)
    calm.step(20.0, 0.0, 0.1)
    assert plant.state.temperature < calm.state.temperature


def test_gantry_profile_has_corners() -> None:
    speed = gantry_speed_profile(300, 0.1)
    assert speed.shape == (300,)
    assert set(np.unique(speed)) == {0.4, 1.0}
    assert (speed == 0.4).mean() == pytest.approx(0.2, abs=0.02)
