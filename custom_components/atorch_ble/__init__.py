"""The atorch_ble integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# TODO(batch-2/04): replace Any with AtorchBleCoordinator
type AtorchBleConfigEntry = ConfigEntry[Any]

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: AtorchBleConfigEntry) -> bool:
    """Set up atorch_ble from a config entry."""
    # Establish hass.data[DOMAIN] slot before any downstream ticket writes to it.
    # Per implementer-locked "hass.data slot ownership" in PROJECT_CONTEXT.md.
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AtorchBleConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AtorchBleConfigEntry) -> None:
    """Remove a config entry."""
    return None
