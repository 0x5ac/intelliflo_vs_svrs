"""The speed number: the wanted percent, kept across restarts."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PumpConfigEntry, PumpData
from .entity import PumpEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PumpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([PumpSpeed(entry.runtime_data, entry.entry_id)])


class PumpSpeed(PumpEntity, RestoreNumber):
    """0-100% of the pump's 1300-3449 rpm band; 0% stops it even with power on.

    The wanted speed, like the power switch: the percent sensor shows the
    speed the pump has actually reached.
    """

    _attr_name = "speed"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, data: PumpData, entry_id: str) -> None:
        super().__init__(data, entry_id, "number", "speed")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._data.driver.set_percent(last.native_value)

    @property
    def native_value(self) -> int:
        return self._data.state.want_percent

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info(f"percent {int(value)} requested")
        self._data.driver.set_percent(value)
        self._data.watch("percent")
        self.async_write_ha_state()
