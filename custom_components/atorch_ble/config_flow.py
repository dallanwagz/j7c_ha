"""Config flow for the atorch_ble integration.

Implements three steps:

* :py:meth:`AtorchBleConfigFlow.async_step_bluetooth` — HA-triggered entry
  point. Receives a :class:`BluetoothServiceInfoBleak` matching the
  manifest matchers, validates the local name, sets unique_id to the
  normalized MAC, and aborts cleanly on duplicates / unsupported devices.
* :py:meth:`AtorchBleConfigFlow.async_step_bluetooth_confirm` — shows the
  user a confirm prompt with the advertised name + MAC, then creates the
  config entry.
* :py:meth:`AtorchBleConfigFlow.async_step_user` — explicit-abort-only
  step (no form). Exists only to avoid a silent dead-end when the user
  clicks "Add Integration → Atorch BLE" without a prior discovery.

Per the UX lock in PROJECT_CONTEXT.md, discovery is the only successful
path to add a device in v1; manual MAC entry is deferred.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import format_mac
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Defense-in-depth: even though the manifest matcher should filter
# advertisements down to this exact local_name, double-check at the
# config-flow boundary so a future matcher loosening can't silently
# create entries for the wrong device family.
EXPECTED_LOCAL_NAME = "UC96_BLE"


class AtorchBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for atorch_ble."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Bluetooth advertisement matching the manifest matchers."""
        _LOGGER.debug(
            "Received bluetooth discovery: name=%s address=%s",
            discovery_info.name,
            discovery_info.address,
        )

        # Defense-in-depth: refuse advertisements that don't carry the
        # canonical UC96_BLE local name. The manifest matcher should
        # already filter these out, but a future matcher loosening
        # shouldn't be able to leak the wrong family in.
        if discovery_info.name != EXPECTED_LOCAL_NAME:
            integration = await async_get_integration(self.hass, DOMAIN)
            return self.async_abort(
                reason="not_supported_device",
                description_placeholders={
                    "issue_url": integration.manifest.get("issue_tracker", ""),
                },
            )

        normalized_mac = format_mac(discovery_info.address)
        await self.async_set_unique_id(normalized_mac)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the discovered device with the user and create the entry."""
        assert self._discovery_info is not None
        discovery_info = self._discovery_info

        normalized_mac = format_mac(discovery_info.address)
        last4_upper = normalized_mac.replace(":", "")[-4:].upper()
        title = f"Atorch J7-C ({last4_upper})"

        if user_input is not None:
            return self.async_create_entry(
                title=title,
                data={CONF_ADDRESS: normalized_mac},
            )

        self._set_confirm_only()
        placeholders = {
            "name": discovery_info.name,
            "address": normalized_mac,
        }
        self.context["title_placeholders"] = {"name": title}
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explicit-abort-only step.

        v1 has no manual-entry path. Clicking
        "Add Integration → Atorch BLE" from the integrations page with
        no prior discovery lands here; we immediately abort with
        ``discovery_only`` so HA renders the friendly translated copy
        explaining how discovery works instead of a silent dead-end.
        """
        return self.async_abort(reason="discovery_only")
