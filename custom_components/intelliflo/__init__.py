"""Pentair IntelliFlo VS+SVRS behind a Waveshare RS-485 gateway.

The pump itself is driven by intelliflo.py, which knows nothing of Home
Assistant. This module wires it in: the wanted state is set through the
power switch (switch.py) and the speed number (number.py), and the
readings go out through sensor.py.

Unloading, or stopping HA, cancels the server task and leaves the pump as
it is. Left without a controller for long enough the pump stops on its
own (seen during a 7-minute gap). On the next start the switch and number
restore their last state and say what the pump should do. On a first
install there is nothing to restore, so the driver adopts what the pump
is already doing rather than stopping it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_SVRS_RESTART,
    DEFAULT_SVRS_RESTART,
    DOMAIN,
    FAST_REFRESH,
    HEARTBEAT_INTERVAL,
)
from .intelliflo import SETTLE_TIMEOUT, PumpDriver, PumpState

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]


@dataclass
class PumpData:
    """The shared pump state, its driver, and the signal sent when it changes.

    The signal carries one bool: whether the numeric sensors should write.
    `watch(what)` logs, in the background, how the request just made for
    "power" or "percent" settles.
    """

    state: PumpState
    driver: PumpDriver
    signal: str
    watch: Callable[[str], None]


type PumpConfigEntry = ConfigEntry[PumpData]


async def async_setup_entry(hass: HomeAssistant, entry: PumpConfigEntry) -> bool:
    pump_state = PumpState("off")
    signal = f"{DOMAIN}_{entry.entry_id}"
    was_connected = False

    async def on_update(force: bool) -> None:
        # The numeric sensors write on a 5% move, a forced update (every
        # heartbeat, and on losing the link) or a change of connection, so
        # rpm jitter does not flood the recorder. The status always writes;
        # HA drops a write that changes nothing.
        nonlocal was_connected
        numbers = (
            force or pump_state.changed(False) or pump_state.connected != was_connected
        )
        was_connected = pump_state.connected
        if numbers:
            pump_state.snapshot_actual()
        async_dispatcher_send(hass, signal, numbers)

    # Set at setup, changed in the options flow; entries made before the
    # option existed have neither and keep the old behaviour, enabled.
    svrs_restart: bool = entry.options.get(
        CONF_SVRS_RESTART, entry.data.get(CONF_SVRS_RESTART, DEFAULT_SVRS_RESTART)
    )
    driver = PumpDriver(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        pump_state,
        FAST_REFRESH,
        _LOGGER,
        on_update,
        svrs_restart=svrs_restart,
    )
    # Cleared again by the switch or number if either restores a state.
    driver.adopt = True

    def log_outcome(what: str, settled: bool) -> None:
        if settled:
            _LOGGER.info(f"{what} settled: {pump_state.summary()}")
        elif not pump_state.connected:
            _LOGGER.info(f"{what} deferred: pump unreachable, applied on reconnect")
        else:
            _LOGGER.info(
                f"{what} not settled after {SETTLE_TIMEOUT}s: "
                f"{pump_state.phase()}, {pump_state.summary()}"
            )

    async def settle(what: str, settled: Awaitable[bool]) -> None:
        log_outcome(what, await settled)

    @callback
    def watch(what: str) -> None:
        entry.async_create_background_task(
            hass, settle(what, driver.wait_settled()), f"{DOMAIN} {what}"
        )

    entry.runtime_data = PumpData(pump_state, driver, signal, watch)

    # The switch and number restore their state while being added, so this
    # returns with the wanted state in place before the server starts.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def heartbeat(_now: datetime) -> None:
        driver.heartbeat()

    entry.async_on_unload(async_track_time_interval(hass, heartbeat, HEARTBEAT_INTERVAL))
    entry.async_create_background_task(hass, driver.server_run(), f"{DOMAIN} server")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PumpConfigEntry) -> bool:
    # The server task and any pending settle tasks are tied to the entry and
    # cancelled with it; the pump keeps whatever it was doing.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
