"""Pentair IntelliFlo VS+SVRS over a Waveshare RS-485-to-TCP gateway.

No Home Assistant imports: __init__.py wires this in, and the tests run it
bare. Packet dumps log at debug on the "<logger>.packets" child logger.
"""

import asyncio
import enum
import logging
import math
import time
import typing

# fmt: off
CMD_WRITE  = 1
CMD_READ   = 2
CMD_PANEL  = 4
CMD_MODE   = 5
CMD_RUN    = 6
CMD_STATUS = 7
CMD_FLOW   = 9
CMD_RPM    = 10

REPLY_ERROR = 0xFF  # reply type for a rejected command; data[0] is the error code

CMD_MAP = {
    CMD_WRITE:  "WRITE",
    CMD_READ:   "READ",
    CMD_PANEL:  "PANEL",
    CMD_MODE:   "MODE",
    CMD_RUN:    "RUN",
    CMD_STATUS: "STATUS",
    CMD_FLOW:   "FLOW",
    CMD_RPM:    "RPM",
}

HEADER_SIZE   = 9
PACKET_HEADER = (0xFF, 0x00, 0xFF, 0xA5)
PUMP_ADDR     = 0x60  # INTELLIFLO_PUMP_1 in pypentair's address table
OUR_ADDR      = 0x10  # we speak as the SUNTOUCH controller
PANEL_REMOTE  = 0xFF  # CMD_PANEL data: the pump takes commands from us
PANEL_LOCAL   = 0x00  # ... or from its own buttons
REPLY_TIMEOUT = 5
RECONNECT_MIN = 5    # first reopen after a failure; doubles while the pump stays silent
RECONNECT_MAX = 600
STALL_GRACE   = 10   # told to run but at 0 rpm this long after a command or movement: stalled
SETTLE_TIMEOUT    = 600  # a power/percent callback waits this long for the pump to settle
TERMINATE_TIMEOUT = 10   # terminate waits this long for its off command to land

MIN_RPM = 1300
MAX_RPM = 3449

# Status reply data bytes, per pypentair's README. The words are big-endian.
ST_RUN     = 0   # RUN_ON / RUN_OFF: the pump's own run state
ST_MODE    = 1
ST_DRIVE   = 2   # drive state
ST_WATTS   = 3   # word
ST_RPM     = 5   # word
ST_GPM     = 7
ST_PPC     = 8
ST_UNKNOWN = 9
ST_ERROR   = 10
ST_TIMER   = 11  # remaining time, [h, m]
ST_CLOCK   = 13  # [h, m]
ST_SIGNATURE = (ST_RUN, ST_MODE, ST_DRIVE, ST_UNKNOWN, ST_ERROR)  # the non-telemetry bytes

RUN_ON  = 0x0A
RUN_OFF = 0x04

# Register addresses, per pypentair (cilynx/pypentair, pump.py SETTING).
ADDR_ACTUAL_RPM          = 0x0206
ADDR_SVRS_ALARM_AUX      = 0x021A  # pypentair: 0 -> 0x5107 on SVRS alarm, back to 0 on reprime
ADDR_SVRS_ALARM          = 0x021C  # pypentair: 0 -> 0xFFFF on SVRS alarm, back to 0 on reprime
ADDR_TARGET_RPM          = 0x02C4
ADDR_RAMP                = 0x02D1
ADDR_PRIME_DELAY         = 0x02D2  # was 0x03D2, which the pump rejected on every init
ADDR_PRIME_SENSITIVITY   = 0x0317
ADDR_SVRS_RESTART_TIMER  = 0x031A
ADDR_SVRS_RESTART_ENABLE = 0x031B
ADDR_RUNNING_PROGRAM     = 0x0321
ADDR_CELSIUS             = 0x0330
ADDR_PROGRAM_RPM         = 0x038D
ADDR_PRIME_ENABLE        = 0x03B3
ADDR_PRIME_MAX_TIME      = 0x03B5
ADDR_MIN_RPM             = 0x03B6
ADDR_MAX_RPM             = 0x03B7
ADDR_SOFT_PRIME_COUNTER  = 0x03C4

# Written on every connect, so anything changed at the pump's own panel is put
# back once the cable is reconnected. The pump answers error 10 to a value out
# of its range (restart timer 10, restart enable 100, prime max time 1000).
INIT_REGISTERS = {
    "ramp":                (ADDR_RAMP,                100),   # 5-200; 200 is the fastest - how fast get to 2400
    "max_rpm":             (ADDR_MAX_RPM,             MAX_RPM),  # before min: min < max
    "min_rpm":             (ADDR_MIN_RPM,             MIN_RPM),
    "prime_enable":        (ADDR_PRIME_ENABLE,        1),
    "prime_delay":         (ADDR_PRIME_DELAY,       1), # how fast between incs after 2400.
    "prime_max_time":      (ADDR_PRIME_MAX_TIME,      3),
    "prime_sensitivity":   (ADDR_PRIME_SENSITIVITY,   10),
    "svrs_restart_enable": (ADDR_SVRS_RESTART_ENABLE, 1),   # 0/1 only
    "svrs_restart_timer":  (ADDR_SVRS_RESTART_TIMER,  30),  # seconds; 10 rejected
}
# fmt: on

Clock = typing.Callable[[], float]


def within_5pct(a, b):
    return (a * 0.95) < b < (a * 1.05)


def moved(a, b) -> bool:
    """Did a reading change by more than 5%? Equal values never count, 0 included."""
    return a != b and not within_5pct(a, b)


def hex_or_none(v: int | None) -> str:
    return "None" if v is None else f"{v:#06x}"


def packet_to_str(p: bytes, full: bool = False) -> str:
    parts: list[str] = []
    if full:
        parts.append(f"<hdr:{p[0]:02x} {p[1]:02x} {p[2]:02x} {p[3]:02x} {p[4]:02x}> ")
        parts.append(f"<dstsrc:{p[5]:02x} {p[6]:02x}> ")
    parts.append(f"<cmd:{p[7]:02x}> ")
    if full:
        parts.append(f"<len:{p[8]:02x}> ")
    for b in p[9:-2]:
        parts.append(f"{b:02x} ")
    if full:
        parts.append(f"<chk:{p[-2]:02x} {p[-1]:02x}>")
    return "".join(parts)


class PumpConnection:
    """Framed packets over an open gateway socket. Make one with open()."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        log: logging.Logger,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.log = log
        self._packets = log.getChild("packets")

    @classmethod
    async def open(
        cls, host: str, port: int, log: logging.Logger
    ) -> "PumpConnection":
        log.debug(f"connecting to {host}:{port}")
        reader, writer = await asyncio.open_connection(host, port)
        log.info(f"connected to {host}:{port}")
        return cls(reader, writer, log)

    async def close(self) -> None:
        """Close the socket. A dead transport must not raise out of a finally
        and replace the exception, or the cancellation, that got us here."""
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except OSError as e:
            self.log.debug(f"socket already gone on close: {e}")

    async def clear_buffer(self) -> None:
        """Send 0xFF*10 to re-sync pump framing, then drain any stale inbound bytes."""
        self._writer.write(bytes([0xFF] * 10))
        await self._writer.drain()
        await asyncio.sleep(0.2)
        try:
            leftover = await asyncio.wait_for(self._reader.read(256), timeout=0.1)
            if leftover:
                self.log.info(
                    f"discarded {len(leftover)} stale byte(s) on connect: "
                    f"{leftover.hex(' ')}"
                )
        except TimeoutError:
            pass

    async def read_reply(self) -> tuple[int, bytes]:
        """Read one complete reply packet; return (cmd_type, payload).

        Raises SyntaxError on bad header/address, asyncio.IncompleteReadError on
        disconnect, TimeoutError if the whole packet does not arrive within
        REPLY_TIMEOUT. A pump that stops answering holds the socket open, so
        without the deadline the dispatch loop waits on it forever.
        """
        async with asyncio.timeout(REPLY_TIMEOUT):
            header = await self._reader.readexactly(HEADER_SIZE)
            if tuple(header[:4]) != PACKET_HEADER:
                raise SyntaxError("Invalid packet header")
            payload = await self._reader.readexactly(header[8] + 2)
        dst, src, cmd_type = header[5], header[6], header[7]
        if dst != OUR_ADDR or src != PUMP_ADDR:
            raise SyntaxError("Strange src or dst")
        self._packets.debug(f"<<<<< {packet_to_str(header + payload, full=True)}")

        return cmd_type, payload

    async def write_pkt(self, cmd: int, data: list[int]) -> None:
        """Build and transmit a framed packet: header + cmd + data + 2-byte checksum."""
        packet_data = [*PACKET_HEADER, 0x00, PUMP_ADDR, OUR_ADDR, cmd, len(data), *data]
        checksum = sum(packet_data[3:])
        packet = bytes([*packet_data, (checksum >> 8) & 0xFF, checksum & 0xFF])
        self._writer.write(packet)
        self._packets.debug(f">>>>>  {packet_to_str(packet, full=True)}")


class PumpState:
    """What we want the pump to do, what it last reported, and the checks between them.

    Times come from `clock`. While `connected` is False the readings are stale.
    """

    def __init__(self, want_power: str, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        self.want_power: str = want_power
        self.want_percent: int = 0
        self.connected: bool = False
        self.actual_rpm: int = 0
        self.actual_w: int = 0
        self.prev_actual_rpm: int = 0
        self.prev_actual_w: int = 0
        # last status reply: data bytes, then 2 checksum bytes
        self.status_raw: bytes = b""
        self.svrs_alarm: int | None = None
        self.svrs_alarm_aux: int | None = None
        self.commanded_at: float = 0.0  # last time desired state was set or sent
        self.moving_at: float = 0.0  # last time a status reply showed rpm > 0

    def __str__(self) -> str:
        return (
            f"want={self.want_power} {self.want_percent}% ({self.want_rpm()} rpm) "
            f"actual={self.actual_rpm} rpm ({self.actual_percent()}%)"
        )

    def summary(self) -> str:
        """The pump's state in a few words, for the info log."""
        if self.stalled():
            return (
                f"stalled at 0 rpm ({self.status_str()} "
                f"svrs_alarm={hex_or_none(self.svrs_alarm)})"
            )
        if self.actual_rpm == 0:
            return "off"
        return f"on {self.actual_rpm} rpm ({self.actual_percent()}%)"

    def actual_percent(self) -> int:
        """Convert actual RPM to 0-100% of the usable [MIN_RPM, MAX_RPM] band. 0 RPM -> 0%."""
        rpm = self.actual_rpm
        if rpm == 0:
            return 0
        span = MAX_RPM - MIN_RPM
        offset = rpm - MIN_RPM
        pct = math.ceil(offset / span * 100)
        return min(max(0, pct), 100)

    def want_rpm(self) -> int:
        """Convert wanted 0-100% to an absolute RPM in [MIN_RPM, MAX_RPM]. 0% -> 0 RPM."""
        percent = self.want_percent
        if percent == 0:
            return 0
        return int(percent * (MAX_RPM - MIN_RPM) // 100 + MIN_RPM)

    def insync(self) -> bool:
        """Is the pump turning at the wanted speed, or stopped when it should be?

        Judged on rpm alone. The run byte in the status reply is kept for
        HA but decides nothing: a pump wanted off that reports run on at
        0 rpm (started at its own panel, not yet spinning) counts as off
        until it turns, and the heartbeat sends RUN_OFF within 20s anyway.
        """
        # Off, or on at 0%, both mean "not turning"; a 5% band around 0 is empty.
        # The 5% band is on rpm. On percent, a 2% target would accept only
        # exactly 2%.
        if not self.wants_running():
            return self.actual_rpm == 0
        return within_5pct(self.want_rpm(), self.actual_rpm)

    def wants_running(self) -> bool:
        return self.want_power == "on" and self.want_percent != 0

    def note_command(self) -> None:
        """Desired state was just set or sent: restart the stall grace period."""
        self.commanded_at = self.clock()

    def stalled(self) -> bool:
        """Told to run, not turning, and past the grace a normal start needs.

        Resending commands does not help a stalled pump; it restarts on its
        own (seen: ~5s of running, then ~2 min stopped, like an SVRS trip and
        its restart timer). Judged on rpm, like insync(). False while
        disconnected, since then we can't tell.
        """
        return (
            self.connected
            and self.wants_running()
            and self.actual_rpm == 0
            and self.clock() - max(self.commanded_at, self.moving_at) >= STALL_GRACE
        )

    def settled(self) -> bool:
        """Nothing to watch closely: in sync, or stalled and waiting on the pump."""
        return self.insync() or self.stalled()

    def phase(self) -> str:
        """disconnected / off / running / ramping / stopping / stalled, for HA."""
        if not self.connected:
            return "disconnected"
        if self.stalled():
            return "stalled"
        if self.insync():
            return "running" if self.wants_running() else "off"
        return "ramping" if self.wants_running() else "stopping"

    def changed(self, force: bool) -> bool:
        return (
            force
            or moved(self.prev_actual_rpm, self.actual_rpm)
            or moved(self.prev_actual_w, self.actual_w)
        )

    def snapshot_actual(self) -> None:
        self.prev_actual_rpm = self.actual_rpm
        self.prev_actual_w = self.actual_w

    # ------------------------------------------------------------------ status bytes

    def _status_byte(self, i: int) -> int | None:
        return self.status_raw[i] if i < len(self.status_raw) else None

    def _status_word(self, i: int) -> int:
        raw = self.status_raw
        return (raw[i] << 8) | raw[i + 1] if i + 1 < len(raw) else 0

    def status_fields(self) -> dict[str, int | None]:
        """The status bytes not otherwise decoded, named as in pypentair's README."""
        return {
            "run": self._status_byte(ST_RUN),
            "mode": self._status_byte(ST_MODE),
            "drive": self._status_byte(ST_DRIVE),
            "b9": self._status_byte(ST_UNKNOWN),
            "error": self._status_byte(ST_ERROR),
        }

    def status_str(self) -> str:
        """The status_fields in hex, e.g. "run=0a mode=00 drive=02 b9=00 error=00"."""
        return " ".join(
            f"{name}={'--' if v is None else f'{v:02x}'}"
            for name, v in self.status_fields().items()
        )

    def signature(self) -> tuple[int | None, ...]:
        return tuple(self.status_fields().values())

    def apply_status(self, payload: bytes) -> bool:
        """Take a status reply; return True if its non-telemetry bytes changed."""
        before = self.signature()
        self.status_raw = bytes(payload)
        self.actual_w = self._status_word(ST_WATTS)
        self.actual_rpm = self._status_word(ST_RPM)
        if self.actual_rpm > 0:
            self.moving_at = self.clock()
        return self.signature() != before


class Cmd(enum.StrEnum):
    """Jobs the integration queues for the server loop."""

    UPDATE = "update"  # the wanted state changed: apply it, then poll
    HEARTBEAT = "heartbeat"  # periodic: re-apply, sample the SVRS alarms, poll


OnUpdate = typing.Callable[[bool], typing.Awaitable[None]]


class PumpDriver:
    """Talks to the pump: owns the connection, the command queue and the server loop.

    Callers set the wanted state with set_power/set_percent, or with
    send_power_cmd/send_percent_cmd, which also wait for the pump to settle.
    The loop applies it, polls until the pump settles, and calls
    on_update(force) after every poll and whenever the link drops.

    With `adopt` set, the first connection takes the wanted state from what
    the pump is already doing instead of imposing one. That is for a first
    install, when there is no wanted state yet; any set_* clears it.
    """

    _conn: PumpConnection

    def __init__(
        self,
        gateway_host: str,
        gateway_port: int,
        state: PumpState,
        fast_refresh_interval: float,
        log: logging.Logger,
        on_update: OnUpdate,
        svrs_restart: bool = True,
    ) -> None:
        self._gateway_host = gateway_host
        self._gateway_port = gateway_port
        self._state = state
        self._fast_refresh_interval = fast_refresh_interval
        self.log = log
        self._on_update = on_update
        # INIT_REGISTERS, with the SVRS restart enable set from svrs_restart.
        self._registers = dict(INIT_REGISTERS)
        self._registers["svrs_restart_enable"] = (
            ADDR_SVRS_RESTART_ENABLE,
            int(svrs_restart),
        )
        self._dqueue: asyncio.Queue[Cmd] = asyncio.Queue(maxsize=10)
        self._jobs = {
            Cmd.UPDATE: self._do_update,
            Cmd.HEARTBEAT: self._do_heartbeat,
        }
        self._heartbeat_queued = False  # at most one heartbeat waits in the queue
        self._pin_pending = False  # _registers still to write, once the pump stops
        self._answered = False  # the pump replied on the current connection
        self._last_phase: str | None = None  # logged at info when it changes
        self._warned: set[tuple] = set()  # keys _warn_once has already warned for
        self.adopt = False
        # Wanting a state at startup counts as a command: no stall before init.
        self._state.note_command()

    # ------------------------------------------------------------------ the app's side

    def _enqueue(self, cmd: Cmd) -> bool:
        try:
            self._dqueue.put_nowait(cmd)
            self.log.debug(f"queued {cmd.value} ({self._state})")
            return True
        except asyncio.QueueFull:
            self.log.warning(f"queue full, dropped {cmd.value}")
            return False

    async def _dequeue(self) -> Cmd:
        self.log.debug(f"waiting for a command, {self._dqueue.qsize()} queued")
        return await self._dqueue.get()

    def _drop_queued(self) -> None:
        """Forget commands queued while disconnected; init re-applies the wanted state anyway."""
        dropped = 0
        while not self._dqueue.empty():
            self._dqueue.get_nowait()
            dropped += 1
        self._heartbeat_queued = False
        if dropped:
            self.log.debug(f"dropped {dropped} command(s) queued while disconnected")

    def heartbeat(self) -> None:
        if self._heartbeat_queued:
            return  # the queued one has not run yet; another says nothing new
        self._heartbeat_queued = self._enqueue(Cmd.HEARTBEAT)

    async def wait_settled(self, timeout: float = SETTLE_TIMEOUT) -> bool:
        """Wait until the pump is in sync or stalled; True if it got there.

        Gives up, returning False, once the link is down (init enforces the
        wanted state on reconnect) or after `timeout`. It only watches; the
        server loop does the polling.
        """
        clock = self._state.clock
        deadline = clock() + timeout
        while True:
            if self._state.settled():
                return True
            if not self._state.connected or clock() >= deadline:
                return False
            await asyncio.sleep(min(self._fast_refresh_interval, deadline - clock()))

    def set_percent(self, percent) -> None:
        """Set the wanted percent now; the server loop applies it."""
        self._state.want_percent = min(100, int(percent))
        self._state.note_command()
        self.adopt = False
        self._enqueue(Cmd.UPDATE)

    def set_power(self, onoff: str) -> None:
        """Set the wanted power, "on" or "off", now; the server loop applies it."""
        self._state.want_power = onoff
        self._state.note_command()
        self.adopt = False
        self._enqueue(Cmd.UPDATE)

    async def send_percent_cmd(self, percent, timeout: float = SETTLE_TIMEOUT) -> bool:
        self.set_percent(percent)
        return await self.wait_settled(timeout)

    async def send_power_cmd(self, onoff, timeout: float = SETTLE_TIMEOUT) -> bool:
        self.set_power(onoff)
        return await self.wait_settled(timeout)

    # ------------------------------------------------------------------ protocol

    def _parse_reply(self, cmd_type: int, payload: bytes) -> int | None:
        """Dispatch on the reply type, update state.

        Returns the word for a register read and None for anything else. An
        unknown reply to a read is None too, never a sentinel word: 0xFFFF
        is exactly what the SVRS alarm register reads when tripped.
        """
        if cmd_type == CMD_WRITE:
            self.log.debug(f"write acknowledged: {payload.hex(' ')}")
        elif cmd_type == CMD_READ:
            self.log.debug(f"register read: {payload.hex(' ')}")
            return (payload[0] << 8) | (payload[1] << 0)
        elif cmd_type in (CMD_PANEL, CMD_RUN):
            # Both echo the byte we sent, so neither says what the pump is
            # doing; rpm in the status reply does. The panel has no readable
            # state at all (pypentair saw the same: set it, read it back,
            # nothing changed).
            self.log.debug(
                f"{CMD_MAP[cmd_type].lower()} command echoed: {payload[0]:#04x}"
            )
        elif cmd_type == CMD_MODE:
            self.log.debug(f"program reply: {payload.hex(' ')}")
        elif cmd_type == CMD_STATUS:
            if self._state.apply_status(payload):
                self.log.info(
                    f"status bytes changed: {self._state.status_str()} "
                    f"raw {payload.hex(' ')}"
                )
            self.log.debug(f"status {payload.hex(' ')} {self._state}")
        elif cmd_type == CMD_FLOW:
            self.log.debug("flow mode reply")
        elif cmd_type == CMD_RPM:
            self.log.debug("rpm mode reply")
        else:
            self._warn_once(
                ("reply", cmd_type),
                f"unknown reply type {cmd_type:#04x}: {payload.hex(' ')}",
            )
        return None

    def _warn_once(self, key: tuple, msg: str) -> None:
        """Warn the first time `key` is seen, then log repeats at debug.

        The SVRS alarm reads are rejected on every heartbeat.
        """
        if key in self._warned:
            self.log.debug(msg)
        else:
            self._warned.add(key)
            self.log.warning(msg)

    async def _transact(self, cmd: int, data: list[int]) -> int | None:
        """Send one command and parse its reply.

        The register word for a read; None for everything else, including a
        rejected command and a reply type we do not know.
        """
        await self._conn.write_pkt(cmd, data)
        cmd_type, payload = await self._conn.read_reply()
        self._answered = True
        hexdata = " ".join(f"{b:02x}" for b in data)
        if cmd_type == REPLY_ERROR:
            code = payload[0] if payload else None
            self._warn_once(
                (cmd, hexdata, code),
                f"pump rejected [{CMD_MAP[cmd]}] [{hexdata}]: error {code}",
            )
            return None
        result = self._parse_reply(cmd_type, payload)
        self.log.debug(f"transact [{CMD_MAP[cmd]}] [{hexdata}] -> {result}")
        return result

    async def _pump_set_rpm(self, value: int) -> None:
        """Write the target rpm to ADDR_TARGET_RPM; the pump ramps at its configured rate."""
        self.log.debug(f"target rpm {value}")
        addr = ADDR_TARGET_RPM
        await self._transact(
            CMD_RPM, [addr >> 8, addr & 0xFF, (value >> 8) & 0xFF, value & 0xFF]
        )

    async def _pump_write(self, addr: int, value: int) -> None:
        """Write a 16-bit value to a pump register."""
        await self._transact(
            CMD_WRITE,
            [(addr >> 8) & 0xFF, addr & 0xFF, (value >> 8) & 0xFF, value & 0xFF],
        )

    async def _pump_read(self, addr: int) -> int | None:
        """Read a 16-bit pump register; None if the pump rejected the read or
        answered with a reply type we do not know."""
        return await self._transact(CMD_READ, [(addr >> 8) & 0xFF, addr & 0xFF])

    async def _pump_power_on(self) -> None:
        await self._transact(CMD_RUN, [RUN_ON])

    async def _pump_power_off(self) -> None:
        await self._transact(CMD_RUN, [RUN_OFF])

    async def _pump_panel_remote(self) -> None:
        await self._transact(CMD_PANEL, [PANEL_REMOTE])

    async def _pump_panel_local(self) -> None:
        await self._transact(CMD_PANEL, [PANEL_LOCAL])

    # ------------------------------------------------------------------ jobs

    async def _enforce(self) -> None:
        """Make the pump match the wanted power and speed."""
        if self._state.wants_running():
            await self._pump_panel_remote()
            await self._pump_power_on()
            await self._pump_set_rpm(self._state.want_rpm())
        else:
            await self._pump_power_off()
            await self._pump_panel_local()

    async def _poll(self, force_update: bool = False) -> None:
        """Ask for status, note any phase change, push the result to the app.

        Adopting happens here, on the first status after connecting, so the
        app never sees the placeholder wanted state the driver started with.
        """
        self.log.debug("poll status")
        await self._transact(CMD_STATUS, [])
        if self.adopt:
            self._adopt()
        self._note_phase()
        await self._push(force_update)

    def _note_phase(self) -> None:
        """Log the phase at info when it changes."""
        st = self._state
        phase = st.phase()
        if phase != self._last_phase:
            self.log.info(
                f"pump {self._last_phase} -> {phase}: rpm {st.actual_rpm} "
                f"watts {st.actual_w} {st.status_str()}"
            )
            self._last_phase = phase

    async def _push(self, force: bool) -> None:
        """Hand the state to on_update. If that raises, log it and keep the connection."""
        try:
            await self._on_update(force)
        except Exception as e:
            self.log.error(f"HA update failed: {type(e).__name__}: {e}")

    async def _verify_config(self) -> None:
        """Read back _registers and the SVRS alarms; log them.

        Warns for each _registers value the pump did not keep.
        """
        st = self._state
        values = {
            name: await self._pump_read(addr)
            for name, (addr, _want) in self._registers.items()
        }
        await self._read_svrs()
        self.log.info(
            "pump config: "
            + " ".join(f"{name}={v}" for name, v in values.items())
            + f" svrs_alarm={hex_or_none(st.svrs_alarm)}"
            + f" svrs_alarm_aux={hex_or_none(st.svrs_alarm_aux)}"
        )
        for name, (_addr, want) in self._registers.items():
            if values[name] != want:
                self.log.warning(f"pump kept {name}={values[name]}, wanted {want}")

    async def _read_svrs(self) -> None:
        """Sample the SVRS alarm registers; pypentair saw 0xFFFF / 0x5107 while tripped."""
        st = self._state
        before = (st.svrs_alarm, st.svrs_alarm_aux)
        st.svrs_alarm = await self._pump_read(ADDR_SVRS_ALARM)
        st.svrs_alarm_aux = await self._pump_read(ADDR_SVRS_ALARM_AUX)
        if (st.svrs_alarm, st.svrs_alarm_aux) != before:
            self.log.info(
                f"svrs alarm {hex_or_none(st.svrs_alarm)} "
                f"aux {hex_or_none(st.svrs_alarm_aux)} "
                f"(was {hex_or_none(before[0])} {hex_or_none(before[1])}); "
                f"rpm {st.actual_rpm} {st.status_str()}"
            )

    async def _pin_registers(self) -> None:
        """Write _registers and read them back.

        Only while the pump is stopped: a running pump answers error 11 to
        every one of these writes.
        """
        for addr, value in self._registers.values():
            await self._pump_write(addr, value)
        await self._verify_config()
        self._pin_pending = False

    async def _do_init(self) -> None:
        """Every connection starts here: resync the framing, take the panel,
        pin the registers if the pump is stopped, then apply the wanted state.

        No RUN of its own: _enforce sets power, and a flapping link must not
        start a pump that is wanted off every 5s. A pump found running (after
        an HA restart, or a cable plugged back in) gets its registers pinned
        by the first heartbeat after it stops.
        """
        await self._conn.clear_buffer()
        await self._pump_panel_remote()
        await self._poll()
        if self._state.actual_rpm == 0:
            await self._pin_registers()
        else:
            self._pin_pending = True
            self.log.info(
                f"pump is running at {self._state.actual_rpm} rpm; "
                "register pinning deferred until it stops"
            )
        await self._do_update()

    def _adopt(self) -> None:
        """Want what the pump is doing: on at its current speed, or off.

        Stopped, it keeps the wanted percent it had (0 on a first install).
        """
        st = self._state
        if st.actual_rpm > 0:
            st.want_power = "on"
            st.want_percent = st.actual_percent()
        else:
            st.want_power = "off"
        self.adopt = False
        self.log.info(f"adopted the pump's own state: {st}")

    async def _do_update(self) -> None:
        """The wanted state changed: apply it, then poll."""
        self._state.note_command()
        await self._enforce()
        await self._poll()

    async def _do_heartbeat(self) -> None:
        """Re-apply the wanted state, sample the SVRS alarms, poll.

        Not a command for the stall grace: it repeats every 20s and would
        hold the grace open forever. The forced update lets readings that
        drifted by less than the 5% change gate catch up.
        """
        self._heartbeat_queued = False
        if self._pin_pending and self._state.actual_rpm == 0:
            await self._pin_registers()
        await self._enforce()
        await self._read_svrs()
        await self._poll(force_update=True)

    async def _run(self, cmd: Cmd) -> None:
        self.log.debug(f"run {cmd.value} ({self._state})")
        await self._jobs[cmd]()

    # ------------------------------------------------------------------ server

    async def _next_cmd(self) -> Cmd | None:
        """The next queued command, or None when a poll is due instead.

        While the pump converges, a poll is due whenever the queue stays quiet
        for fast_refresh_interval. Once it has settled (in sync, or stalled and
        waiting on its own restart) only commands and the heartbeat wake it,
        after one last poll so the log and HA show the settled phase.
        """
        st = self._state
        if st.settled():
            if st.phase() != self._last_phase:
                return None
            return await self._dequeue()
        try:
            return await asyncio.wait_for(self._dequeue(), self._fast_refresh_interval)
        except TimeoutError:
            return None

    async def _serve_connection(self) -> None:
        """Open a connection, init the pump, then serve commands and polls.

        Init runs directly rather than through the queue, so a full queue
        cannot drop it and stale commands cannot run ahead of it.

        The loop only ends by raising (disconnect, or no reply within
        REPLY_TIMEOUT) or by task cancellation; the connection is closed on
        the way out either way.
        """
        self._conn = await PumpConnection.open(
            self._gateway_host, self._gateway_port, self.log
        )
        self._state.connected = True
        self._drop_queued()
        try:
            await self._do_init()
            while True:
                cmd = await self._next_cmd()
                if cmd is None:
                    await self._poll()
                else:
                    await self._run(cmd)
        finally:
            self._state.connected = False
            await self._conn.close()

    def _failure_message(self, e: Exception) -> str:
        """One line per way the link can fail. TimeoutError is checked
        before OSError, its parent since 3.11."""
        known = (
            (ConnectionRefusedError, "gateway refused the connection"),
            (ConnectionResetError, "gateway reset the connection"),
            (asyncio.IncompleteReadError, "gateway closed the connection mid-reply"),
            (SyntaxError, "lost packet framing"),
            (TimeoutError, f"no reply from pump within {REPLY_TIMEOUT}s"),
            (OSError, f"cannot reach {self._gateway_host}:{self._gateway_port}"),
        )
        for cls, text in known:
            if isinstance(e, cls):
                return f"{text}: {e}" if str(e) else text
        return f"server loop failed: {type(e).__name__}: {e}"

    async def server_run(self) -> None:
        """Serve connections forever, reopening after each failure.

        The reopen delay starts at RECONNECT_MIN and doubles up to
        RECONNECT_MAX while the pump stays silent; a connection on which
        the pump answered resets it.
        """
        delay = RECONNECT_MIN
        while True:
            self._answered = False
            try:
                await self._serve_connection()
            except Exception as e:
                self.log.error(self._failure_message(e))

            # Tell the app the link is down; HA shows "disconnected" rather
            # than the last reading for the whole outage.
            self._note_phase()
            await self._push(True)

            if self._answered:
                delay = RECONNECT_MIN
            self.log.info(f"reconnecting in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)
