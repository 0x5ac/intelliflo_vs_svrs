"""The power switch: whether the pump should run, kept across restarts."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import PumpConfigEntry, PumpData
from .entity import PumpEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PumpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([PumpPower(entry.runtime_data, entry.entry_id)])


class PumpPower(PumpEntity, SwitchEntity, RestoreEntity):
    """On: the pump should run, at the speed number's percent.

    This is the wanted power, not the pump's own; the status sensor shows
    what the pump is doing. It changes under the user only when the driver
    adopts the pump's state on a first install.
    """

    _attr_name = "power"

    def __init__(self, data: PumpData, entry_id: str) -> None:
        super().__init__(data, entry_id, "switch", "power")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._data.driver.set_power(last.state)

    @property
    def is_on(self) -> bool:
        return self._data.state.want_power == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._request("on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._request("off")

    def _request(self, onoff: str) -> None:
        _LOGGER.info(f"power {onoff} requested")
        self._data.driver.set_power(onoff)
        self._data.watch("power")
        self.async_write_ha_state()
