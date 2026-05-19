"""Config flow for the atorch_ble integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class AtorchBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for atorch_ble."""

    VERSION = 1
    # TODO(batch-2/02): implement bluetooth discovery + user steps
