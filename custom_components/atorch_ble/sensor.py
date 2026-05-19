"""Sensor platform for the atorch_ble integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up atorch_ble sensors from a config entry."""
    # TODO(batch-3/01): build entity descriptions, instantiate sensors
    return None
