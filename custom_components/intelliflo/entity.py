"""Base for the pump's entities: one device per entry, pushed by the driver's signal."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import PumpData
from .const import DOMAIN, OBJECT_ID_PREFIX


class PumpEntity(Entity):
    """Named under the device, with ids <platform>.<prefix>_<key>.

    Every signal from the driver writes the state; subclasses that should
    not write on every one override _on_update.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, data: PumpData, entry_id: str, platform: str, key: str) -> None:
        self._data = data
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Courtyard fall",
            manufacturer="Pentair",
            model="IntelliFlo VS+SVRS",
        )
        self.entity_id = f"{platform}.{OBJECT_ID_PREFIX}_{key}"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._data.signal, self._on_update)
        )

    @callback
    def _on_update(self, numbers: bool) -> None:
        self.async_write_ha_state()
