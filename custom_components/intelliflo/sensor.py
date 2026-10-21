"""Sensors returned by the pump: rpm, watts. And the pump's constructed state and %."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, REVOLUTIONS_PER_MINUTE, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PumpConfigEntry, PumpData
from .entity import PumpEntity
from .intelliflo import PumpState

PHASES = ["disconnected", "off", "running", "ramping", "stopping", "stalled"]


@dataclass(frozen=True, kw_only=True)
class ReadingDescription(SensorEntityDescription):
    value_fn: Callable[[PumpState], int]


READINGS = (
    ReadingDescription(
        key="rpm",
        name="rpm",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda st: st.actual_rpm,
    ),
    ReadingDescription(
        key="w",
        name="w",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda st: st.actual_w,
    ),
    ReadingDescription(
        key="percent",
        name="percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda st: st.actual_percent(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PumpConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[PumpSensor] = [
        PumpReading(data, entry.entry_id, description) for description in READINGS
    ]
    entities.append(PumpStatus(data, entry.entry_id))
    async_add_entities(entities)


class PumpSensor(PumpEntity, SensorEntity):
    """Base for the readings and the status."""

    def __init__(self, data: PumpData, entry_id: str, key: str) -> None:
        super().__init__(data, entry_id, "sensor", key)


class PumpReading(PumpSensor):
    """A numeric reading; unavailable while the pump is unreachable."""

    entity_description: ReadingDescription

    def __init__(
        self, data: PumpData, entry_id: str, description: ReadingDescription
    ) -> None:
        super().__init__(data, entry_id, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return self._data.state.connected

    @property
    def native_value(self) -> int:
        return self.entity_description.value_fn(self._data.state)

    @callback
    def _on_update(self, numbers: bool) -> None:
        if numbers:
            self.async_write_ha_state()


class PumpStatus(PumpSensor):
    """The phase, with the raw status bytes and SVRS alarms as attributes.

    Always available: an unreachable pump reads "disconnected".
    """

    _attr_name = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PHASES

    def __init__(self, data: PumpData, entry_id: str) -> None:
        super().__init__(data, entry_id, "status")

    @property
    def native_value(self) -> str:
        return self._data.state.phase()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._data.state
        return {
            **st.status_fields(),
            "svrs_alarm": st.svrs_alarm,
            "svrs_alarm_aux": st.svrs_alarm_aux,
        }
