# Pentair IntelliFlo VS+SVRS for Home Assistant


## This integration or the IntelliCenter one?

Pentair pumps are often run by an automation panel such as the IntelliCenter, and the [IntelliCenter integration](https://github.com/joyfulhouse/intellicenter) talks to that panel over your network.
That panel owns the pump.
That integration shows the panel's readings for it and edits the panel's per-circuit speed settings, and the pump runs whenever a circuit or body that uses it is on.

This integration is for a pump with no panel.
It takes the panel's place on the pump's RS-485 bus and drives the pump itself.

|               | This integration                  | IntelliCenter integration                     |
| ------------- | --------------------------------- | --------------------------------------------- |
| Talks to      | the pump, over RS-485             | the IntelliCenter panel, over your LAN        |
| Pumps         | one per gateway                   | every pump the panel knows                    |
| On/off        | power switch                      | none; follows the panel's circuits and bodies |
| Speed         | 0-100% slider over 1300-3449 rpm  | rpm or gpm setpoint per circuit               |
| Readings      | rpm, W, percent, status           | rpm, W, gpm, running                          |
| Faults        | stalls, status bytes, SVRS alarms | none                                          |
| Pump settings | written on every connect (below)  | min/max rpm read only; priming speed and time |

Never run both on one pump: the pump would take orders from two controllers on one bus.



A Home Assistant integration that runs on my Pentair IntelliFlo VS+SVRS pump over RS-485 bus.
Quoting to google, it should also work on:

```

• IntelliFlo & IntelliPro series (IntelliFlo VSF, IntelliFlo3 with proper communication setup, etc.)
• WhisperFlo VST
• SuperFlo VST (manufactured after November 20, 2020)
• SuperMax VS (newer VST/VS iterations)
```
But not on pumps without RS-485, eg
```
• Early SuperFlo VS: Units made prior to November 20, 2020 feature a local digital display/keypad but no RS-485 port. (Newer versions like the SuperFlo VST added RS-485).
• Standard Single-Speed Pumps: Traditional single-speed models (e.g., WhisperFlo, SuperFlo, SuperMax, and Sta-Rite equivalents) run via basic mechanical or external timer switches rather than digital data buses.
• Pumps Lacking Automation Drives: Any Pentair variable-speed pump that has been retrofitted with a third-party or basic non-communicating drive un

```
I get to RS-485 through a generic TCP<>RS-485 gateway, so Home Assistant talks plain TCP and needs no serial hardware of its own.

The integration acts as the pump's controller.
It sets power and speed and every so often reports what the pump is doing.
Whilst in control the pump the panel on the pump is disabled.
You get it back when the pump is turned off in the integration.



## Example Dashboard

`examples/dashboard.yaml` is a one-page dashboard for the pump, built from core cards only: the power and speed controls, the live readings, a day's history and the status sensor's diagnostic attributes.
HACS installs only `custom_components/intelliflo`, so take it from this repository.

The integration creates every entity the page uses; there are no helpers to make.
To add it by hand, create a dashboard, open its raw configuration editor and paste the file in.
If Home Assistant gave an entity a `_2` suffix, because something else had its id first, fix that id in the page.

Or let the script do it.
It finds the integration's entities by their unique ids, puts their real ids into the page, and creates the dashboard through Home Assistant's websocket API, as the UI would:

```sh
HA_TOKEN=<long-lived access token> uv run examples/add_dashboard.py \
    --url http://homeassistant.local:8123
```

Make the token on your user profile's Security tab.
`--dry-run` prints the page and changes nothing; `--force` replaces a copy added earlier.
Without uv, `pip install pyyaml websockets` and run it with python.

I've got HA_TOKEN set in my .bashrc, my home assistant server is called ha, so I can
install the dashboard with

```
examples/add_dashboard.py --url http://ha:8123
```

## Hardware

- A Pentair IntelliFlo VS+SVRS on its RS-485 terminals.
- TCP<>RS-485 gateway wired to the same pair, set to 9600 baud, 8N1

On my machine the gateway serves runs on port 4196.

## Installation

With [HACS](https://hacs.xyz): add `https://github.com/0x5ac/intelliflo_vs_svrs` as a custom repository of type Integration, install it, and restart Home Assistant.

By hand: copy `custom_components/intelliflo` into your Home Assistant `config/custom_components/` directory and restart.

## Setup

Add the integration under Settings > Devices & services and give it the gateway's host and port.
It brings its own power switch and speed slider.
Turning the switch on or moving the slider sends the command, and the status sensor shows the pump ramping until it gets there.

On its first connect the integration takes over whatever the pump is already doing: a running pump keeps running at its current speed, and a stopped one stays off.
After that the switch and slider are in charge.

Speed in percent maps linearly onto 1300-3449 rpm, and 0% means 0 rpm, so the pump stays stopped at 0% even with the power switch on.

The same form has an SVRS restart switch, on by default.
With it on, the pump restarts itself 30 seconds after a safety vacuum release trip; with it off, the pump stays stopped.
To change it later, open the integration's Configure dialog.
The integration reloads on save, and the pump gets the new setting as soon as it is stopped.

## Entities

| Entity         | Meaning                                                   |
| -------------- | --------------------------------------------------------- |
| power (switch) | whether the pump should run                               |
| speed (number) | the speed it should run at, 0-100%                        |
| rpm            | measured speed                                            |
| w              | power draw, in W                                          |
| percent        | measured speed as a percentage of the 1300-3449 rpm range |
| status         | disconnected, off, running, ramping, stopping or stalled  |

The switch and slider hold what you asked for; the sensors show what the pump is doing.
A command given while the pump cannot be reached is applied when it comes back.

The three readings go unavailable while the pump cannot be reached, and they only update on a change of more than 5% or every 20 seconds, which keeps the recorder quiet.
The status sensor carries the raw status fields and the two SVRS alarm registers as attributes.

## Settings written to the pump

Each time the integration connects, it writes these registers, so changes made at the pump's own panel are put back.
A running pump refuses the writes, so if it is running they wait until it next stops.

| Setting             | Value                         |
| ------------------- | ----------------------------- |
| ramp                | 100                           |
| max rpm             | 3449                          |
| min rpm             | 1300                          |
| prime enable        | 1                             |
| prime delay         | 1                             |
| prime max time      | 3                             |
| prime sensitivity   | 10                            |
| SVRS restart enable | 1, or 0 with SVRS restart off |
| SVRS restart timer  | 30 s                          |

They live in `INIT_REGISTERS` in `intelliflo.py`.

## Behaviour on restart

Unloading the integration, or stopping Home Assistant, leaves the pump doing whatever it was doing.
Left without a controller for long enough, the pump stops by itself.
The power switch and speed slider restore their last state at startup, and the integration drives the pump back to them.

## Tests

The tests cover the pump protocol and state handling in `intelliflo.py` and need only pytest, not Home Assistant:

```sh
pytest
```

## Credits

- Register addresses and the status layout come from [pypentair](https://github.com/cilynx/pypentair).

- Claude did lots of worrying about my code.  He's awesome.
  
