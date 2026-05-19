"""Tests for the atorch_ble sensor platform.

Covers all ten sensors (five primary + temperature + runtime + two USB
data-line voltages + diagnostic connection_state), default-enabled vs.
default-disabled flags, value extraction from a synthesized
``AtorchBleData`` snapshot, and the device-class / state-class /
entity-category attribute matrix.
"""

from __future__ import annotations

from unittest.mock import patch

from atorch_ble import UsbMeterReading
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atorch_ble.const import CONNECTION_STATES, DOMAIN
from custom_components.atorch_ble.coordinator import AtorchBleData

from .conftest import TEST_MAC_NORMALIZED, TEST_TITLE


# Map sensor description key -> (device_class, state_class, entity_category, enabled_by_default)
_EXPECTED_KEYS = {
    "voltage",
    "current",
    "power",
    "energy",
    "capacity",
    "voltage_dplus",
    "voltage_dminus",
    "temperature",
    "runtime",
    "connection_state",
}

_DEFAULT_ENABLED = {
    "voltage",
    "current",
    "power",
    "energy",
    "capacity",
    "temperature",
    "runtime",
}

_DEFAULT_DISABLED = {"voltage_dplus", "voltage_dminus", "connection_state"}

_TOTAL_INCREASING = {"energy", "capacity", "runtime"}


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MAC_NORMALIZED,
        data={CONF_ADDRESS: TEST_MAC_NORMALIZED},
        title=TEST_TITLE,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = _make_entry(hass)
    with patch(
        "custom_components.atorch_ble.coordinator.AtorchBleCoordinator._start_runner"
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
    return entry


def _unique_id_for(key: str) -> str:
    mac_stripped = TEST_MAC_NORMALIZED.replace(":", "")
    return f"{mac_stripped}_{key}"


async def test_all_sensors_present(hass: HomeAssistant) -> None:
    """All 10 sensor entities are registered after setup."""
    entry = await _setup(hass)

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    keys_seen = {e.unique_id.split("_", 1)[1] for e in entries}
    assert keys_seen == _EXPECTED_KEYS


async def test_default_enabled_sensors(hass: HomeAssistant) -> None:
    """Default-enabled vs. default-disabled flag matrix is correct."""
    await _setup(hass)
    registry = er.async_get(hass)

    for key in _DEFAULT_ENABLED:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, _unique_id_for(key)
        )
        assert entity_id is not None, f"missing entity for {key}"
        entry_obj = registry.async_get(entity_id)
        assert entry_obj.disabled_by is None, f"{key} should be enabled"

    for key in _DEFAULT_DISABLED:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, _unique_id_for(key)
        )
        assert entity_id is not None, f"missing entity for {key}"
        entry_obj = registry.async_get(entity_id)
        assert entry_obj.disabled_by is not None, f"{key} should be disabled"


async def test_sensor_values_from_reading(
    hass: HomeAssistant,
) -> None:
    """Each sensor's value_fn extracts the expected field from AtorchBleData.

    Exercises the entity-description value-source contract directly
    against an ``AtorchBleData`` snapshot, sidestepping HA's
    state-machine plumbing — the per-description ``value_fn`` /
    ``value_fn_coordinator`` callables are the load-bearing surface for
    "what value does the sensor report?".
    """
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    reading = UsbMeterReading(
        voltage_v=5.123,
        current_a=1.500,
        capacity_mah=200,
        energy_wh=12.50,
        voltage_dplus_v=2.1,
        voltage_dminus_v=2.2,
        temperature_c=30,
        duration_s=120,
    )
    snapshot = AtorchBleData(
        reading=reading,
        power_w=reading.voltage_v * reading.current_a,
    )

    from custom_components.atorch_ble.sensor import DESCRIPTIONS

    by_key = {d.key: d for d in DESCRIPTIONS}
    assert by_key["voltage"].value_fn(snapshot) == 5.123
    assert by_key["current"].value_fn(snapshot) == 1.5
    assert abs(by_key["power"].value_fn(snapshot) - 5.123 * 1.5) < 1e-6
    assert by_key["energy"].value_fn(snapshot) == 12.5
    assert by_key["capacity"].value_fn(snapshot) == 200
    assert by_key["temperature"].value_fn(snapshot) == 30
    assert by_key["runtime"].value_fn(snapshot) == 120
    assert by_key["voltage_dplus"].value_fn(snapshot) == 2.1
    assert by_key["voltage_dminus"].value_fn(snapshot) == 2.2

    # connection_state reads from the coordinator object directly.
    assert by_key["connection_state"].value_fn_coordinator(coordinator) == (
        coordinator.connection_state
    )


async def test_state_class_total_increasing(hass: HomeAssistant) -> None:
    """energy/capacity/runtime carry TOTAL_INCREASING; voltage carries MEASUREMENT."""
    from custom_components.atorch_ble.sensor import DESCRIPTIONS

    by_key = {d.key: d for d in DESCRIPTIONS}
    for key in _TOTAL_INCREASING:
        assert by_key[key].state_class == SensorStateClass.TOTAL_INCREASING

    assert by_key["voltage"].state_class == SensorStateClass.MEASUREMENT
    assert by_key["current"].state_class == SensorStateClass.MEASUREMENT
    assert by_key["power"].state_class == SensorStateClass.MEASUREMENT
    assert by_key["temperature"].state_class == SensorStateClass.MEASUREMENT


async def test_device_class_correct(hass: HomeAssistant) -> None:
    """Per-sensor device_class matches the inventory."""
    from custom_components.atorch_ble.sensor import DESCRIPTIONS

    by_key = {d.key: d for d in DESCRIPTIONS}
    assert by_key["voltage"].device_class == SensorDeviceClass.VOLTAGE
    assert by_key["current"].device_class == SensorDeviceClass.CURRENT
    assert by_key["power"].device_class == SensorDeviceClass.POWER
    assert by_key["energy"].device_class == SensorDeviceClass.ENERGY
    assert by_key["temperature"].device_class == SensorDeviceClass.TEMPERATURE
    assert by_key["runtime"].device_class == SensorDeviceClass.DURATION
    assert by_key["voltage_dplus"].device_class == SensorDeviceClass.VOLTAGE
    assert by_key["voltage_dminus"].device_class == SensorDeviceClass.VOLTAGE
    assert by_key["connection_state"].device_class == SensorDeviceClass.ENUM
    # capacity has no canonical mAh device_class in HA yet.
    assert by_key["capacity"].device_class is None


async def test_entity_state_propagation_through_ha_pipeline(
    hass: HomeAssistant,
) -> None:
    """A coordinator update propagates to HA state via the listener pipeline.

    This is the real-HA test that the previous value_fn-direct workaround
    was hiding: ``CoordinatorEntity.async_added_to_hass`` subscribes via
    ``coordinator.async_add_listener``, the coordinator's
    ``async_set_updated_data`` notifies subscribers, and each entity's
    ``async_write_ha_state`` lands the new value in ``hass.states``.

    Covers the freshness-window availability rule, the value_fn branch,
    and the value_fn_coordinator diagnostic branch — all driven by the
    real entity lifecycle, not direct ``native_value`` calls.
    """
    from datetime import datetime, timedelta, timezone

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # No reading yet -> voltage sensor reports "unavailable"; the
    # diagnostic connection_state sensor is enabled-by-default false so
    # it would not be in hass.states; assert via the coordinator path.
    voltage_state = hass.states.get("sensor.atorch_j7_c_eeff_voltage")
    assert voltage_state is not None
    assert voltage_state.state == "unavailable"

    # Publish a reading via the real coordinator pipeline.
    reading = UsbMeterReading(
        voltage_v=5.0,
        current_a=1.0,
        capacity_mah=10,
        energy_wh=1.0,
        voltage_dplus_v=2.5,
        voltage_dminus_v=2.5,
        temperature_c=20,
        duration_s=10,
    )
    coordinator._last_reading = reading
    coordinator._last_seen = datetime.now(timezone.utc)
    snapshot = AtorchBleData(reading=reading, power_w=5.0)
    coordinator.async_set_updated_data(snapshot)
    await hass.async_block_till_done()

    voltage_state = hass.states.get("sensor.atorch_j7_c_eeff_voltage")
    assert voltage_state.state == "5.0"
    power_state = hass.states.get("sensor.atorch_j7_c_eeff_power")
    assert power_state.state == "5.0"

    # Backdate last_seen well past the freshness window. Re-publishing
    # the snapshot drives a fresh write_ha_state cycle through the
    # listener pipeline, and the availability flips to unavailable.
    coordinator._last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
    coordinator.async_set_updated_data(snapshot)
    await hass.async_block_till_done()
    voltage_state = hass.states.get("sensor.atorch_j7_c_eeff_voltage")
    assert voltage_state.state == "unavailable"


async def test_native_value_branches(hass: HomeAssistant) -> None:
    """``native_value`` covers both value-source branches + the no-data case.

    The HA-pipeline test above drives the value_fn happy path, but the
    diagnostic ``value_fn_coordinator`` sensor is disabled-by-default
    and would not be in ``hass.states``. This test exercises the entity
    accessor directly to keep coverage on the diagnostic branch and the
    ``data is None`` short-circuit.
    """
    from custom_components.atorch_ble.sensor import (
        DESCRIPTIONS,
        AtorchBleSensor,
    )

    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    by_key = {d.key: d for d in DESCRIPTIONS}
    voltage_sensor = AtorchBleSensor(coordinator, by_key["voltage"])
    conn_state_sensor = AtorchBleSensor(coordinator, by_key["connection_state"])

    # value_fn branch with no data yet -> None / unavailable.
    assert coordinator.data is None
    assert voltage_sensor.native_value is None

    # value_fn_coordinator branch -> always reads coordinator state.
    assert conn_state_sensor.available is True
    assert conn_state_sensor.native_value == coordinator.connection_state


async def test_listener_removal_is_idempotent(hass: HomeAssistant) -> None:
    """The unsubscribe callback returned by async_add_listener is idempotent.

    Mirrors ``DataUpdateCoordinator``'s contract: calling the remover
    twice is harmless. Guards the shim against a stale unsubscribe
    handle that fires after the entity has already been removed.
    """
    entry = await _setup(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    calls: list[None] = []

    def _on_update() -> None:
        calls.append(None)

    remove = coordinator.async_add_listener(_on_update)
    coordinator._async_notify_listeners()
    assert len(calls) == 1
    remove()
    coordinator._async_notify_listeners()
    assert len(calls) == 1  # no further calls after removal
    # Second remove is a no-op, not an error.
    remove()


async def test_connection_state_enum_options(hass: HomeAssistant) -> None:
    """connection_state sensor exposes the closed-set options + DIAGNOSTIC category."""
    from custom_components.atorch_ble.sensor import DESCRIPTIONS

    by_key = {d.key: d for d in DESCRIPTIONS}
    desc = by_key["connection_state"]
    assert set(desc.options) == set(CONNECTION_STATES)
    assert desc.entity_category == EntityCategory.DIAGNOSTIC
    assert desc.entity_registry_enabled_default is False
