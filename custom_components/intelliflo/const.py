"""Constants for the Pentair IntelliFlo VS+SVRS integration."""

from datetime import timedelta

DOMAIN = "intelliflo"

CONF_SVRS_RESTART = "svrs_restart"

DEFAULT_HOST = "fish_waveshare_waterfall"
DEFAULT_PORT = 4196
DEFAULT_SVRS_RESTART = True  # the pump restarts itself after an SVRS trip

FAST_REFRESH = 1.0  # seconds between polls while the pump converges
HEARTBEAT_INTERVAL = timedelta(seconds=20)

# Entity ids are <platform>.<prefix>_<key>: sensor.pond_pump_rpm,
# switch.pond_pump_power and so on. These are what examples/dashboard.yaml
# uses. An install that already has its entities keeps their ids.
OBJECT_ID_PREFIX = "pond_pump"
