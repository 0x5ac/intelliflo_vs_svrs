# Changelog

Notable changes to this integration, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2026-10-28]

### Added
- examples: a one-page pump dashboard built from core cards only, and
  `add_dashboard.py`, which creates it in a running Home Assistant
  through the websocket API, using that install's real entity ids
- README: how this integration differs from the IntelliCenter one, and
  how to add the example dashboard
- LICENSE: GPL v2

### Fixed
- README: setup told you to create an input_boolean and an
  input_number first; the integration has its own power switch and
  speed slider, and the README now says so
- manifest: the documentation, issue tracker and code owner were
  placeholder OWNER links that went nowhere; they now point at
  github.com/0x5ac/intelliflo_vs_svrs

### Changed
- entities: a new install gets `pond_pump_*` entity ids, matching the
  example dashboard, instead of `courtyard_fall_*`; an existing install
  keeps the ids it has
- README: links the author's own copy of the example dashboard, and the
  HACS steps say "this repository" instead of spelling out its URL
