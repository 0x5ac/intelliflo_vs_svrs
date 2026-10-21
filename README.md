# Pentair IntelliFlo VS+SVRS for Home Assistant

A Home Assistant integration that runs on my Pentair IntelliFlo VS+SVRS 
pump over RS-485 bus.  Quoting to google, it should also work on:

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

The integration acts as the pump's controller. It sets power and speed
and every so often reports what the pump is doing. Whilst in control the pump
the panel on the pump is disabled.  You get it back when the pump is turned off
in the integration.

## Hardware

- A Pentair IntelliFlo VS+SVRS on its RS-485 terminals.
- TCP<>RS-485 gateway wired to the same pair, set to
  9600 baud, 8N1


The gateway serves one client at a time. Port 4196.


## Installation

With [HACS](https://hacs.xyz): add this repository as a custom
repository of type Integration, install it, and restart Home Assistant.

By hand: copy `custom_components/intelliflo` into your Home Assistant
`config/custom_components/` directory and restart.

## Setup

Create two helpers first. They hold the state you want the pump in:

- an `input_boolean` for power;
- an `input_number` from 0 to 100 for speed, in percent.

Then add the integration under Settings > Devices & services and give
it the gateway's host and port and the two helpers. Turning the switch
on or moving the slider sends the command, and the status sensor shows
the pump ramping until it gets there.

Speed in percent maps linearly onto 1300-3449 rpm, and 0% means 0 rpm.

The same form has an SVRS restart switch, on by default. With it on,
the pump restarts itself 30 seconds after a safety vacuum release trip;
with it off, the pump stays stopped. To change it later, open the
integration's Configure dialog. The integration reloads on save, and the
pump gets the new setting as soon as it is stopped.

## Entities

| Entity  | Meaning                                                   |
| ------- | --------------------------------------------------------- |
| rpm     | measured speed                                            |
| w       | power draw, in W                                          |
| percent | measured speed as a percentage of the 1300-3449 rpm range |
| status  | disconnected, off, running, ramping, stopping or stalled  |

The three readings go unavailable while the pump cannot be reached, and
they only update on a change of more than 5% or every 20 seconds, which
keeps the recorder quiet. The status sensor carries the raw status
fields and the two SVRS alarm registers as attributes.

## Settings written to the pump

Each time the integration connects, it writes these registers, so
changes made at the pump's own panel are put back. A running pump
refuses the writes, so if it is running they wait until it next stops.

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

Unloading the integration, or stopping Home Assistant, leaves the pump
doing whatever it was doing. Left without a controller for long enough,
the pump stops by itself. Home Assistant restores the two helpers at
startup, and the integration drives the pump back to them.

## Tests

The tests cover the pump protocol and state handling in `intelliflo.py`
and need only pytest, not Home Assistant:

```sh
pytest
```

## Credits

Register addresses and the status layout come from
[pypentair](https://github.com/cilynx/pypentair).
