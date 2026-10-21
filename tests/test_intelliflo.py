"""Tests for intelliflo.py -- state math, packet framing, and the pump driver.

No Home Assistant required: intelliflo.py does not import it, and the
integration glue in __init__.py and sensor.py is not exercised here.
PumpDriver is driven against a fake connection, and anything timed runs on
a FakeClock.

Claude used with pride.

"""

import asyncio
import typing
from unittest.mock import MagicMock

import pytest
from intelliflo import intelliflo
from intelliflo.intelliflo import (
    ADDR_PRIME_DELAY,
    ADDR_SVRS_ALARM,
    ADDR_SVRS_ALARM_AUX,
    ADDR_SVRS_RESTART_ENABLE,
    CMD_PANEL,
    CMD_READ,
    CMD_RPM,
    CMD_RUN,
    CMD_STATUS,
    CMD_WRITE,
    INIT_REGISTERS,
    MAX_RPM,
    MIN_RPM,
    RUN_OFF,
    RUN_ON,
    STALL_GRACE,
    Cmd,
    PumpConnection,
    PumpDriver,
    PumpState,
    moved,
    packet_to_str,
    within_5pct,
)


def run(coro):
    """Run one coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


def drain(queue):
    """Pop every currently-queued item without blocking."""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


class FakeClock:
    """A clock the test moves by hand: `clock.now = 10.0`."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# within_5pct, moved
# ---------------------------------------------------------------------------


class TestWithin5pct:
    def test_exact_match(self):
        assert within_5pct(1000, 1000)

    def test_inside_band(self):
        assert within_5pct(1000, 1040)
        assert within_5pct(1000, 960)

    def test_outside_band(self):
        assert not within_5pct(1000, 1060)
        assert not within_5pct(1000, 940)

    def test_boundaries_exclusive(self):
        # band is strict: (a*0.95) < b < (a*1.05)
        assert not within_5pct(1000, 950)
        assert not within_5pct(1000, 1050)

    def test_zero_reference_never_matches_nonzero(self):
        # 0 < b < 0 is always false for a == 0
        assert not within_5pct(0, 0)
        assert not within_5pct(0, 10)


class TestMoved:
    def test_equal_is_not_moved_even_at_zero(self):
        assert not moved(0, 0)
        assert not moved(2000, 2000)

    def test_inside_band_is_not_moved(self):
        assert not moved(1000, 1040)

    def test_from_zero_is_moved(self):
        assert moved(0, 10)
        assert moved(10, 0)


# ---------------------------------------------------------------------------
# packet_to_str
# ---------------------------------------------------------------------------


class TestPacketToStr:
    def test_renders_fields(self):
        pkt = bytes(
            [
                0xFF,
                0x00,
                0xFF,
                0xA5,
                0x00,
                0x60,
                0x10,
                0x07,
                0x02,
                0xAB,
                0xCD,
                0x01,
                0x1C,
            ]
        )
        out = packet_to_str(pkt, full=True)
        assert "<cmd:07>" in out
        assert "<len:02>" in out
        assert "ab " in out and "cd " in out
        assert "<chk:01 1c>" in out

    def test_short_form_is_command_and_data(self):
        pkt = bytes(
            [
                0xFF,
                0x00,
                0xFF,
                0xA5,
                0x00,
                0x60,
                0x10,
                0x07,
                0x02,
                0xAB,
                0xCD,
                0x01,
                0x1C,
            ]
        )
        assert packet_to_str(pkt) == "<cmd:07> ab cd "


# ---------------------------------------------------------------------------
# PumpState
# ---------------------------------------------------------------------------


class TestPumpStatePercentRpm:
    def test_actual_percent_zero_rpm(self):
        st = PumpState("off")
        st.actual_rpm = 0
        assert st.actual_percent() == 0

    def test_actual_percent_min_rpm(self):
        st = PumpState("on")
        st.actual_rpm = MIN_RPM
        assert st.actual_percent() == 0

    def test_actual_percent_max_rpm(self):
        st = PumpState("on")
        st.actual_rpm = MAX_RPM
        assert st.actual_percent() == 100

    def test_actual_percent_clamped(self):
        st = PumpState("on")
        st.actual_rpm = MAX_RPM + 5000
        assert st.actual_percent() == 100

    def test_want_rpm_zero_percent(self):
        st = PumpState("on")
        st.want_percent = 0
        assert st.want_rpm() == 0

    def test_want_rpm_full(self):
        st = PumpState("on")
        st.want_percent = 100
        assert st.want_rpm() == MAX_RPM

    def test_want_rpm_in_band(self):
        st = PumpState("on")
        st.want_percent = 50
        rpm = st.want_rpm()
        assert MIN_RPM <= rpm <= MAX_RPM


class TestPumpStateSync:
    def test_off_insync_when_stopped(self):
        st = PumpState("off")
        st.actual_rpm = 0
        assert st.insync()

    def test_off_not_insync_while_spinning(self):
        st = PumpState("off")
        st.actual_rpm = MAX_RPM
        assert not st.insync()

    def test_off_ignores_the_run_byte(self):
        # started at the pump's own panel, not yet turning: still off
        st = PumpState("off")
        st.apply_status(status_reply(run=RUN_ON, rpm=0)[1])
        assert st.insync()
        assert st.status_fields()["run"] == RUN_ON  # kept for HA all the same

    def test_on_zero_percent_is_insync_when_stopped(self):
        # a 5% band around 0 is empty; "on at 0%" has to mean "not turning"
        st = PumpState("on")
        st.want_percent = 0
        st.actual_rpm = 0
        assert st.insync()

    def test_on_zero_percent_not_insync_while_spinning(self):
        st = PumpState("on")
        st.want_percent = 0
        st.actual_rpm = 2000
        assert not st.insync()

    def test_on_matching_percent_insync(self):
        st = PumpState("on")
        st.want_percent = 50
        st.actual_rpm = st.want_rpm()
        assert st.insync()

    def test_low_speed_jitter_is_insync(self):
        # 2% is 1342 rpm; the band is +-5% of that, not of the percent
        st = PumpState("on")
        st.want_percent = 2
        st.actual_rpm = st.want_rpm() + 40
        assert st.insync()
        st.actual_rpm = st.want_rpm() + 80
        assert not st.insync()

    def test_off_not_insync_until_stopped(self):
        # below MIN_RPM actual_percent() reads 0, but it is still turning
        st = PumpState("off")
        st.actual_rpm = 1200
        assert not st.insync()
        st.actual_rpm = 0
        assert st.insync()

    def test_on_mismatched_percent_not_insync(self):
        st = PumpState("on")
        st.want_percent = 80
        st.actual_rpm = MIN_RPM
        assert not st.insync()


class TestPumpStateChange:
    def test_force_reports_changed(self):
        st = PumpState("off")
        assert st.changed(True)

    def test_no_change_after_snapshot(self):
        st = PumpState("off")
        st.actual_rpm = 2000
        st.actual_w = 300
        st.snapshot_actual()
        assert not st.changed(False)

    def test_no_change_while_stopped(self):
        st = PumpState("off")  # rpm 0, watts 0, snapshot 0, 0
        assert not st.changed(False)

    def test_rpm_delta_reports_changed(self):
        st = PumpState("off")
        st.actual_rpm = 2000
        st.actual_w = 300
        st.snapshot_actual()
        st.actual_rpm = 2500
        assert st.changed(False)

    def test_summary_off(self):
        st = PumpState("off")
        assert st.summary() == "off"

    def test_summary_on(self):
        st = PumpState("on")
        st.actual_rpm = MAX_RPM
        assert st.summary() == f"on {MAX_RPM} rpm (100%)"

    def test_summary_stalled(self):
        st, clock = stalled_state()
        clock.now = STALL_GRACE
        assert st.summary().startswith("stalled at 0 rpm")


def stalled_state() -> tuple[PumpState, FakeClock]:
    """Connected, wants 57%, at 0 rpm, last commanded at t=0 on a FakeClock."""
    clock = FakeClock(0.0)
    st = PumpState("on", clock=clock)
    st.connected = True
    st.want_percent = 57
    st.note_command()
    return st, clock


class TestPumpStateStall:
    def test_not_stalled_inside_the_grace(self):
        st, clock = stalled_state()
        clock.now = STALL_GRACE - 1
        assert not st.stalled()
        assert st.phase() == "ramping"
        assert not st.settled()

    def test_stalled_after_the_grace(self):
        st, clock = stalled_state()
        clock.now = STALL_GRACE
        assert st.stalled()
        assert st.phase() == "stalled"
        assert st.settled()

    def test_movement_restarts_the_grace(self):
        st, clock = stalled_state()
        clock.now = 100.0
        st.apply_status(status_reply(run=RUN_ON, rpm=1300)[1])
        clock.now = 105.0
        st.apply_status(status_reply(run=RUN_ON, rpm=0)[1])
        clock.now = 100.0 + STALL_GRACE - 1
        assert not st.stalled()
        clock.now = 100.0 + STALL_GRACE
        assert st.stalled()

    def test_reported_run_state_does_not_matter(self):
        st, clock = stalled_state()
        # however the pump reports itself, it is not turning
        st.apply_status(status_reply(run=RUN_OFF, rpm=0)[1])
        clock.now = STALL_GRACE
        assert st.stalled()

    def test_never_stalled_when_not_wanted_running(self):
        st, clock = stalled_state()
        clock.now = 1000.0
        st.want_power = "off"
        assert not st.stalled()
        st.want_power = "on"
        st.want_percent = 0
        assert not st.stalled()

    def test_unreachable_is_not_stalled(self):
        st, clock = stalled_state()
        clock.now = 1000.0
        st.connected = False
        assert not st.stalled()
        assert st.phase() == "disconnected"

    def test_phases(self):
        st, clock = stalled_state()
        clock.now = 1000.0
        st.actual_rpm = st.want_rpm()
        assert st.phase() == "running"
        st.want_power = "off"
        assert st.phase() == "stopping"
        st.actual_rpm = 0
        assert st.phase() == "off"


class TestPumpStateStatusBytes:
    def test_apply_status_decodes_the_full_reply(self):
        st = PumpState("on", clock=FakeClock(5.0))
        _, payload = status_reply(run=RUN_ON, rpm=1300, watts=141, drive=0x02)
        st.apply_status(payload)
        assert (st.actual_rpm, st.actual_w) == (1300, 141)
        assert st.moving_at == 5.0
        assert st.status_fields() == {
            "run": 0x0A,
            "mode": 0,
            "drive": 2,
            "b9": 0,
            "error": 0,
        }
        assert st.status_str() == "run=0a mode=00 drive=02 b9=00 error=00"

    def test_apply_status_reports_only_signature_changes(self):
        st = PumpState("on")
        assert st.apply_status(status_reply(run=RUN_ON, rpm=1200)[1])
        assert not st.apply_status(status_reply(run=RUN_ON, rpm=1300)[1])
        assert st.apply_status(status_reply(run=RUN_ON, error=4)[1])

    def test_short_reply_leaves_missing_fields_none(self):
        st = PumpState("off")
        st.apply_status(bytes([0, 0, 0, 0x01, 0x00, 0x0C, 0x00]))
        assert st.actual_rpm == 3072
        assert st.status_fields()["error"] is None
        assert "error=--" in st.status_str()


# ---------------------------------------------------------------------------
# PumpConnection -- framing over fake reader/writer
# ---------------------------------------------------------------------------


class FakeReader:
    def __init__(self, data: bytes = b""):
        self._buf = bytearray(data)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def read(self, n: int) -> bytes:
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class FakeWriter:
    def __init__(self, dead: bool = False):
        self.buf = bytearray()
        self.closed = False
        self.dead = dead  # wait_closed raises, as on a transport that already failed

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self.dead:
            raise ConnectionResetError("Connection reset by peer")


def stream_conn(reader, writer) -> PumpConnection:
    """A PumpConnection over duck-typed stand-ins for the asyncio streams."""
    return PumpConnection(
        typing.cast(asyncio.StreamReader, reader),
        typing.cast(asyncio.StreamWriter, writer),
        MagicMock(),
    )


def make_conn(reader_data: bytes = b""):
    reader = FakeReader(reader_data)
    writer = FakeWriter()
    return stream_conn(reader, writer), reader, writer


class TestPumpConnection:
    def test_write_pkt_frames_with_checksum(self):
        conn, _reader, writer = make_conn()
        run(conn.write_pkt(CMD_STATUS, []))
        # header + cmd + len + 2-byte checksum; checksum = sum(bytes[3:])
        expected = bytes(
            [0xFF, 0x00, 0xFF, 0xA5, 0x00, 0x60, 0x10, CMD_STATUS, 0, 0x01, 0x1C]
        )
        assert bytes(writer.buf) == expected

    def test_read_reply_parses_valid_packet(self):
        header = bytes([0xFF, 0x00, 0xFF, 0xA5, 0x00, 0x10, 0x60, 0x07, 0x07])
        payload = bytes([0, 0, 0, 0x01, 0x00, 0x0C, 0x00])
        checksum = bytes([0x00, 0x00])
        conn, _reader, _writer = make_conn(header + payload + checksum)
        cmd_type, body = run(conn.read_reply())
        assert cmd_type == 0x07
        # body is payload + checksum bytes (read does not verify checksum)
        assert body[3] == 0x01 and body[5] == 0x0C

    def test_read_reply_bad_header_raises(self):
        conn, _reader, _writer = make_conn(bytes([0x00] * 11))
        with pytest.raises(SyntaxError):
            run(conn.read_reply())

    def test_read_reply_bad_addr_raises(self):
        # valid magic header, but dst/src are wrong
        header = bytes([0xFF, 0x00, 0xFF, 0xA5, 0x00, 0x11, 0x60, 0x07, 0x00])
        conn, _reader, _writer = make_conn(header + bytes([0x00, 0x00]))
        with pytest.raises(SyntaxError):
            run(conn.read_reply())

    def test_clear_buffer_sends_resync(self):
        conn, _reader, writer = make_conn(b"")
        run(conn.clear_buffer())
        assert bytes(writer.buf) == bytes([0xFF] * 10)

    def test_read_reply_times_out_on_silent_pump(self, monkeypatch):
        class SilentReader:
            async def readexactly(self, n):
                await asyncio.Event().wait()  # holds the socket open, never answers

        monkeypatch.setattr(intelliflo, "REPLY_TIMEOUT", 0.01)
        conn = stream_conn(SilentReader(), FakeWriter())
        with pytest.raises(TimeoutError):
            run(conn.read_reply())

    def test_close_does_not_raise_on_a_dead_transport(self):
        # close() runs in a finally; raising there would replace the real cause
        writer = FakeWriter(dead=True)
        conn = stream_conn(FakeReader(), writer)
        run(conn.close())
        assert writer.closed


# ---------------------------------------------------------------------------
# PumpDriver -- against a fake connection
# ---------------------------------------------------------------------------


class FakeConn(PumpConnection):
    """Stands in for PumpConnection; records writes, replays canned replies.

    With fail_after=n it answers the first n reads, then times out the way
    read_reply does when the pump goes quiet. fail_after=0 is a silent pump.
    A subclass only so the driver's _conn accepts it; it never calls
    PumpConnection.__init__ and overrides everything the driver uses.
    """

    def __init__(self, replies=None, fail_after=None):
        self.writes = []
        self.replies = list(replies or [])
        self.cleared = 0
        self.closed = 0
        self.fail_after = fail_after
        self.reads = 0

    async def write_pkt(self, cmd, data):
        self.writes.append((cmd, list(data)))

    async def read_reply(self):
        if self.fail_after is not None and self.reads >= self.fail_after:
            raise TimeoutError
        self.reads += 1
        if self.replies:
            return self.replies.pop(0)
        return (0x01, bytes([0x00, 0x00]))  # benign default ("using ext")

    async def clear_buffer(self):
        self.cleared += 1

    async def close(self):
        self.closed += 1


class TestDriver(PumpDriver):
    """A PumpDriver with typed handles on what the tests fake.

    `mock_log` is the MagicMock behind `log`, `conn` the FakeConn behind
    `_conn` once one is attached, `updates` each on_update's force flag.
    """

    __test__ = False  # not a test class, whatever pytest makes of the name
    mock_log: MagicMock
    conn: FakeConn
    updates: list[bool]


def make_driver(
    state=None, fast=0.0, on_update=None, svrs_restart=True
) -> tuple[TestDriver, PumpState]:
    """A TestDriver; drv.updates records each on_update's force flag."""
    st = state if state is not None else PumpState("off")
    updates: list[bool] = []

    async def record(force=False):
        updates.append(force)

    log = MagicMock()
    drv = TestDriver(
        "host", 1234, st, fast, log, on_update or record, svrs_restart=svrs_restart
    )
    drv.mock_log = log
    drv.updates = updates
    return drv, st


def status_reply(run=RUN_OFF, rpm=0, watts=0, drive=0, error=0):
    """A full-length status reply: 15 data bytes, then 2 checksum bytes."""
    data = [run, 0, drive, watts >> 8, watts & 0xFF, rpm >> 8, rpm & 0xFF]
    data += [0, 0, 0, error, 0, 0, 1, 29]
    return (CMD_STATUS, bytes(data + [0, 0]))


STATUS_REPLY = (0x07, bytes([0, 0, 0, 0x01, 0x00, 0x0C, 0x00]))  # w=256 rpm=3072


class TestPumpDriverQueue:
    def test_enqueue_dequeue_roundtrip(self):
        drv, _ = make_driver()

        async def go():
            drv._enqueue(Cmd.UPDATE)
            return await drv._dequeue()

        assert run(go()) is Cmd.UPDATE

    def test_enqueue_drops_when_full(self):
        drv, _ = make_driver()
        results = [drv._enqueue(Cmd.UPDATE) for _ in range(15)]  # maxsize is 10
        # over-full puts are dropped, not raised
        assert drv._dqueue.qsize() == 10
        assert results == [True] * 10 + [False] * 5

    def test_heartbeat_enqueues(self):
        drv, _ = make_driver()
        drv.heartbeat()
        assert drain(drv._dqueue) == [Cmd.HEARTBEAT]

    def test_second_heartbeat_waits_for_the_first_to_run(self):
        drv, _ = make_driver()
        drv.heartbeat()
        drv.heartbeat()
        assert drain(drv._dqueue) == [Cmd.HEARTBEAT]
        drv.conn = drv._conn = FakeConn()
        run(drv._run(Cmd.HEARTBEAT))  # the queued one ran
        drv.heartbeat()
        assert drain(drv._dqueue) == [Cmd.HEARTBEAT]

    def test_drop_queued_empties_the_queue(self):
        drv, _ = make_driver()
        drv.heartbeat()
        drv._enqueue(Cmd.UPDATE)
        drv._drop_queued()
        assert drain(drv._dqueue) == []
        drv.heartbeat()  # not blocked by the dropped one
        assert drain(drv._dqueue) == [Cmd.HEARTBEAT]

    def test_send_percent_sets_want_and_enqueues(self):
        # start already in sync (off/0) so the wait returns at once
        drv, st = make_driver()
        assert run(drv.send_percent_cmd(0)) is True
        assert st.want_percent == 0
        assert drain(drv._dqueue) == [Cmd.UPDATE]

    def test_send_power_sets_want(self):
        drv, st = make_driver()
        assert run(drv.send_power_cmd("off")) is True
        assert st.want_power == "off"
        assert drain(drv._dqueue) == [Cmd.UPDATE]

    def test_send_power_restarts_the_stall_grace(self):
        st = PumpState("off")
        st.connected = True
        st.want_percent = 50
        drv, _ = make_driver(st)
        st.commanded_at = -100.0  # long ago

        async def go():
            task = asyncio.create_task(drv.send_power_cmd("on"))
            await asyncio.sleep(0)  # 0 rpm and just commanded: still waiting
            assert not task.done()
            task.cancel()

        run(go())
        assert st.commanded_at > -100.0

    def test_wait_gives_up_when_disconnected(self):
        st = PumpState("off")
        st.want_percent = 50  # the "on" below wants it running; nothing can answer
        drv, _ = make_driver(st)
        assert run(drv.send_power_cmd("on")) is False
        assert st.want_power == "on"  # init enforces it on reconnect

    def test_wait_gives_up_after_the_timeout(self):
        st = PumpState("off")
        st.connected = True
        st.want_percent = 50
        drv, _ = make_driver(st, fast=0.005)
        assert run(drv.send_power_cmd("on", timeout=0.03)) is False


class TestPumpDriverParseReply:
    def test_status_updates_state(self):
        drv, st = make_driver()
        assert drv._parse_reply(*STATUS_REPLY) is None  # only reads return a value
        assert st.actual_w == 256
        assert st.actual_rpm == 3072

    def test_status_run_byte_is_kept(self):
        drv, st = make_driver()
        drv._parse_reply(*status_reply(run=RUN_ON))
        assert st.status_fields()["run"] == RUN_ON
        drv._parse_reply(*status_reply(run=RUN_OFF))
        assert st.status_fields()["run"] == RUN_OFF

    def test_run_echo_is_not_a_status(self):
        # the RUN reply echoes what we sent; it is not the pump's run state
        drv, st = make_driver()
        drv._parse_reply(CMD_RUN, bytes([RUN_ON]))
        assert st.status_fields()["run"] is None

    def test_status_byte_change_is_logged_at_info(self):
        drv, _ = make_driver()
        drv._parse_reply(*status_reply(run=RUN_ON, rpm=1200))
        drv.mock_log.info.reset_mock()
        drv._parse_reply(*status_reply(run=RUN_ON, rpm=1300))  # telemetry only
        assert not drv.mock_log.info.called
        drv._parse_reply(*status_reply(run=RUN_ON, drive=0xFF))
        assert "drive=ff" in drv.mock_log.info.call_args.args[0]

    def test_read_returns_word(self):
        drv, _ = make_driver()
        assert drv._parse_reply(CMD_READ, bytes([0x12, 0x34])) == (0x12 << 8) | 0x34

    def test_unknown_reply_returns_none(self):
        # not 0xFFFF: that is what the SVRS alarm register reads when tripped
        drv, _ = make_driver()
        assert drv._parse_reply(0x7F, bytes([0x00])) is None


class TestPumpDriverTransact:
    def test_transact_writes_and_parses(self):
        drv, st = make_driver()
        drv.conn = drv._conn = FakeConn(replies=[STATUS_REPLY])
        result = run(drv._transact(CMD_STATUS, []))
        assert result is None
        assert drv.conn.writes == [(CMD_STATUS, [])]
        assert st.actual_rpm == 3072

    def test_error_reply_returns_none_and_warns(self):
        drv, _ = make_driver()
        drv.conn = drv._conn = FakeConn(replies=[(0xFF, bytes([10, 0, 0]))])
        assert run(drv._transact(CMD_WRITE, [0x02, 0xD2, 0, 0])) is None
        assert "error 10" in drv.mock_log.warning.call_args.args[0]

    def test_repeated_rejection_warns_once(self):
        drv, _ = make_driver()
        error = (0xFF, bytes([8, 0, 0]))
        drv.conn = drv._conn = FakeConn(replies=[error, error])
        run(drv._pump_read(0x021C))
        run(drv._pump_read(0x021C))
        assert drv.mock_log.warning.call_count == 1
        assert "[02 1c]: error 8" in drv.mock_log.warning.call_args.args[0]

    def test_unknown_reply_to_a_read_is_none(self):
        drv, st = make_driver()
        drv.conn = drv._conn = FakeConn(replies=[(0x7F, bytes([0, 0, 0]))])
        assert run(drv._pump_read(ADDR_SVRS_ALARM)) is None
        assert st.svrs_alarm is None

    def test_phase_change_is_logged_at_info_once(self):
        st = PumpState("on")
        st.connected = True
        st.want_percent = 57
        drv, _ = make_driver(st)
        drv.conn = drv._conn = FakeConn(
            replies=[status_reply(run=RUN_ON, rpm=1300)] * 2
        )
        run(drv._poll())
        run(drv._poll())
        phases = [
            c.args[0] for c in drv.mock_log.info.call_args_list if "->" in c.args[0]
        ]
        assert phases == [
            "pump None -> ramping: rpm 1300 watts 0 run=0a mode=00 drive=00 b9=00 error=00"
        ]

    def test_update_failure_does_not_reach_the_connection(self):
        async def broken(_force=False):
            raise RuntimeError("HA is down")

        drv, _ = make_driver(on_update=broken)
        drv.conn = drv._conn = FakeConn(replies=[status_reply()])
        run(drv._poll())  # must not raise
        assert "HA update failed" in drv.mock_log.error.call_args.args[0]

    def test_read_returns_register_word(self):
        drv, _ = make_driver()
        drv.conn = drv._conn = FakeConn(replies=[(0x02, bytes([0x00, 0x78, 0, 0]))])
        assert run(drv._pump_read(0x031A)) == 120
        assert drv.conn.writes == [(CMD_READ, [0x03, 0x1A])]


class TestPumpDriverJobs:
    def test_poll_asks_once_and_pushes(self):
        drv, _st = make_driver()  # want off, but reply says spinning
        drv.conn = drv._conn = FakeConn(replies=[STATUS_REPLY])
        run(drv._poll())
        # the next poll is _next_cmd's job; nothing is queued for it
        assert drain(drv._dqueue) == []
        assert drv.conn.writes == [(CMD_STATUS, [])]
        assert drv.updates == [False]

    def test_update_to_off_powers_down_and_hands_the_panel_back(self):
        # all terminate() needs: it sets want_power off and queues an update
        st = PumpState("off")
        drv, _ = make_driver(st)
        drv.conn = drv._conn = FakeConn()
        run(drv._run(Cmd.UPDATE))
        cmds = [c for c, _ in drv.conn.writes]
        assert cmds == [CMD_RUN, CMD_PANEL, CMD_STATUS]
        assert drv.conn.writes[0] == (CMD_RUN, [0x04])  # power off
        assert drv.conn.writes[1] == (CMD_PANEL, [0x00])  # panel local

    def test_update_enforces_on_state(self):
        st = PumpState("on")
        st.want_percent = 50
        drv, _ = make_driver(st)
        drv.conn = drv._conn = FakeConn()
        st.commanded_at = -100.0
        run(drv._run(Cmd.UPDATE))
        cmds = [c for c, _ in drv.conn.writes]
        # panel remote, power on, set rpm, then poll
        assert cmds == [CMD_PANEL, CMD_RUN, CMD_RPM, CMD_STATUS]
        assert drv.conn.writes[0] == (CMD_PANEL, [0xFF])
        assert drv.conn.writes[1] == (CMD_RUN, [0x0A])
        assert drain(drv._dqueue) == []
        assert st.commanded_at > -100.0  # an update restarts the stall grace

    def test_heartbeat_reads_svrs_and_keeps_the_grace(self):
        st = PumpState("on")
        st.want_percent = 50
        drv, _ = make_driver(st)
        drv.conn = drv._conn = FakeConn(
            replies=[
                (0x04, bytes([0xFF, 0, 0])),
                (0x06, bytes([RUN_ON, 0, 0])),
                (0x0A, bytes([0, 0, 0])),
                (0x02, bytes([0xFF, 0xFF, 0, 0])),
                (0x02, bytes([0x51, 0x07, 0, 0])),
                status_reply(run=RUN_ON),
            ]
        )
        st.commanded_at = -100.0
        run(drv._run(Cmd.HEARTBEAT))
        reads = [d for c, d in drv.conn.writes if c == CMD_READ]
        assert reads == [
            [ADDR_SVRS_ALARM >> 8, ADDR_SVRS_ALARM & 0xFF],
            [ADDR_SVRS_ALARM_AUX >> 8, ADDR_SVRS_ALARM_AUX & 0xFF],
        ]
        assert (st.svrs_alarm, st.svrs_alarm_aux) == (0xFFFF, 0x5107)
        assert drv.conn.writes[-1] == (CMD_STATUS, [])
        assert drv.updates == [True]  # forced, so HA entities survive an HA restart
        # a heartbeat resends every 20s; it must not hold the grace open
        assert st.commanded_at == -100.0

    def test_init_clears_and_configures(self):
        drv, _ = make_driver()  # want off
        drv.conn = drv._conn = FakeConn()
        run(drv._do_init())
        assert drv.conn.cleared == 1
        # the register values are experiment knobs; only the addresses are pinned
        written = [d[:2] for c, d in drv.conn.writes if c == CMD_WRITE]
        assert [0x02, 0xD2] in written  # PRIME_DELAY, at pypentair's address
        assert ADDR_PRIME_DELAY == 0x02D2
        reads = [d for c, d in drv.conn.writes if c == CMD_READ]
        assert (
            len(reads) == len(INIT_REGISTERS) + 2
        )  # read-back, then the two SVRS alarms
        # panel, a poll to see the pump is stopped, then straight into the
        # register writes: no RUN_ON for a pump that is wanted off; then
        # enforce-off (power off, panel local) and a poll
        assert drv.conn.writes[:2] == [(CMD_PANEL, [0xFF]), (CMD_STATUS, [])]
        assert drv.conn.writes[2][0] == CMD_WRITE
        assert (CMD_RUN, [RUN_ON]) not in drv.conn.writes
        assert drv.conn.writes[-3:] == [
            (CMD_RUN, [RUN_OFF]),
            (CMD_PANEL, [0x00]),
            (CMD_STATUS, []),
        ]
        assert drain(drv._dqueue) == []

    def test_init_defers_pinning_while_the_pump_runs(self):
        drv, st = make_driver()  # want off
        benign = (0x01, bytes([0, 0]))
        # panel echo, poll: running; then enforce-off (run, panel), poll: stopped
        drv.conn = drv._conn = FakeConn(
            replies=[
                benign,
                status_reply(run=RUN_ON, rpm=2420),
                benign,
                benign,
                status_reply(run=RUN_OFF),
            ]
        )
        run(drv._do_init())
        assert [c for c, _ in drv.conn.writes if c == CMD_WRITE] == []
        assert drv._pin_pending
        assert any("deferred" in c.args[0] for c in drv.mock_log.info.call_args_list)
        assert st.actual_rpm == 0  # the enforce-off went through regardless

    def test_heartbeat_pins_once_the_pump_has_stopped(self):
        drv, _ = make_driver()  # want off, rpm 0
        drv._pin_pending = True
        drv.conn = drv._conn = FakeConn()
        run(drv._run(Cmd.HEARTBEAT))
        written = [d for c, d in drv.conn.writes if c == CMD_WRITE]
        assert len(written) == len(INIT_REGISTERS)
        assert not drv._pin_pending
        # the writes come first, while the pump is known to be stopped
        assert drv.conn.writes[0][0] == CMD_WRITE

    def test_heartbeat_keeps_waiting_while_the_pump_runs(self):
        drv, st = make_driver()
        drv._pin_pending = True
        st.actual_rpm = 2420
        drv.conn = drv._conn = FakeConn()
        run(drv._run(Cmd.HEARTBEAT))
        assert [c for c, _ in drv.conn.writes if c == CMD_WRITE] == []
        assert drv._pin_pending

    def test_init_powers_on_only_through_enforce(self):
        st = PumpState("on")
        st.want_percent = 50
        drv, _ = make_driver(st)
        drv.conn = drv._conn = FakeConn()
        run(drv._do_init())
        runs = [d for c, d in drv.conn.writes if c == CMD_RUN]
        assert runs == [[RUN_ON]]  # once, from the enforce step

    def test_init_writes_every_pinned_register(self):
        drv, _ = make_driver()
        drv.conn = drv._conn = FakeConn()
        run(drv._do_init())
        written = [d for c, d in drv.conn.writes if c == CMD_WRITE]
        assert written == [
            [addr >> 8, addr & 0xFF, value >> 8, value & 0xFF]
            for addr, value in INIT_REGISTERS.values()
        ]

    def test_config_readback_warns_on_values_the_pump_kept(self):
        drv, _ = make_driver()
        want = {name: value for name, (_a, value) in INIT_REGISTERS.items()}
        got = dict(want, svrs_restart_timer=120)  # the pump kept its own value
        replies = [
            (0x02, bytes([got[n] >> 8, got[n] & 0xFF, 0, 0])) for n in INIT_REGISTERS
        ]
        replies += [(0x02, bytes([0, 0, 0, 0]))] * 2  # the SVRS alarm reads
        drv.conn = drv._conn = FakeConn(replies=replies)
        run(drv._verify_config())
        assert [c.args[0] for c in drv.mock_log.warning.call_args_list] == [
            "pump kept svrs_restart_timer=120, wanted 30"
        ]

    def test_svrs_restart_off_writes_enable_0(self):
        drv, _ = make_driver(svrs_restart=False)
        drv.conn = drv._conn = FakeConn()
        run(drv._do_init())
        written = [d for c, d in drv.conn.writes if c == CMD_WRITE]
        addr = ADDR_SVRS_RESTART_ENABLE
        assert [addr >> 8, addr & 0xFF, 0, 0] in written
        assert [addr >> 8, addr & 0xFF, 0, 1] not in written
        assert len(written) == len(INIT_REGISTERS)  # the timer is still written

    def test_svrs_restart_on_writes_enable_1(self):
        drv, _ = make_driver()
        drv.conn = drv._conn = FakeConn()
        run(drv._do_init())
        written = [d for c, d in drv.conn.writes if c == CMD_WRITE]
        addr = ADDR_SVRS_RESTART_ENABLE
        assert [addr >> 8, addr & 0xFF, 0, 1] in written

    def test_svrs_restart_off_readback_wants_0(self):
        drv, _ = make_driver(svrs_restart=False)
        got = {name: value for name, (_a, value) in INIT_REGISTERS.items()}  # enable 1
        replies = [
            (0x02, bytes([got[n] >> 8, got[n] & 0xFF, 0, 0])) for n in INIT_REGISTERS
        ]
        replies += [(0x02, bytes([0, 0, 0, 0]))] * 2  # the SVRS alarm reads
        drv.conn = drv._conn = FakeConn(replies=replies)
        run(drv._verify_config())
        assert [c.args[0] for c in drv.mock_log.warning.call_args_list] == [
            "pump kept svrs_restart_enable=1, wanted 0"
        ]

    def test_adopt_keeps_a_running_pump_running(self):
        st = PumpState("off")  # a first install, nothing restored
        pushed = []

        async def record(force=False):
            pushed.append(st.want_power)

        drv, _ = make_driver(st, on_update=record)
        drv.adopt = True
        benign = (0x01, bytes([0, 0]))
        drv.conn = drv._conn = FakeConn(
            replies=[benign, status_reply(run=RUN_ON, rpm=2420)]
        )
        run(drv._do_init())
        assert pushed and set(pushed) == {"on"}  # the app never saw "off"
        assert (st.want_power, st.want_percent) == ("on", st.actual_percent())
        assert st.insync()
        assert not drv.adopt
        assert (CMD_RUN, [RUN_OFF]) not in drv.conn.writes
        assert (CMD_RUN, [RUN_ON]) in drv.conn.writes

    def test_adopt_leaves_a_stopped_pump_off(self):
        st = PumpState("on")
        st.want_percent = 40
        drv, _ = make_driver(st)
        drv.adopt = True
        drv.conn = drv._conn = FakeConn()  # the poll shows 0 rpm
        run(drv._do_init())
        assert (st.want_power, st.want_percent) == ("off", 40)
        assert (CMD_RUN, [RUN_ON]) not in drv.conn.writes

    def test_without_adopt_init_imposes_the_wanted_state(self):
        drv, st = make_driver()  # want off
        benign = (0x01, bytes([0, 0]))
        drv.conn = drv._conn = FakeConn(
            replies=[benign, status_reply(run=RUN_ON, rpm=2420)]
        )
        run(drv._do_init())
        assert st.want_power == "off"
        assert (CMD_RUN, [RUN_OFF]) in drv.conn.writes

    def test_set_power_and_percent_apply_at_once_and_cancel_adopt(self):
        drv, st = make_driver()
        drv.adopt = True
        drv.set_power("on")
        assert st.want_power == "on"
        assert not drv.adopt
        drv.adopt = True
        drv.set_percent(150)
        assert st.want_percent == 100
        assert not drv.adopt
        assert drain(drv._dqueue) == [Cmd.UPDATE, Cmd.UPDATE]

    def test_next_cmd_polls_while_converging(self):
        st = PumpState("on")
        st.connected = True
        st.want_percent = 50  # wants running, rpm 0, just commanded
        drv, _ = make_driver(st, fast=0.01)
        st.note_command()
        assert run(drv._next_cmd()) is None

    def test_next_cmd_polls_once_more_for_the_settled_phase(self):
        drv, st = make_driver(fast=0.01)  # want off, rpm 0: in sync
        st.connected = True
        assert drv._last_phase is None  # "off" has not been logged or drawn yet
        assert run(drv._next_cmd()) is None

    def test_next_cmd_waits_on_the_queue_once_settled(self):
        drv, st = make_driver(fast=0.01)  # want off, rpm 0: in sync
        st.connected = True
        drv._last_phase = "off"

        async def go():
            task = asyncio.create_task(drv._next_cmd())
            await asyncio.sleep(0.05)  # several poll intervals: no poll
            assert not task.done()
            drv._enqueue(Cmd.UPDATE)
            return await task

        assert run(go()) is Cmd.UPDATE


# ---------------------------------------------------------------------------
# PumpDriver.server_run -- reopen and backoff
# ---------------------------------------------------------------------------


class _Stop(Exception):
    """Raised by the patched sleep to end server_run; intelliflo.py never catches it."""


def serve(monkeypatch, conns, stop_after, drv: TestDriver | None = None):
    """Run server_run with one FakeConn per open; return (driver, reopen delays).

    The reopen sleep sits outside server_run's try, so _Stop from the
    stop_after'th sleep ends the loop.
    """
    if drv is None:
        drv, _ = make_driver()
    opened = iter(conns)
    sleeps = []

    async def fake_open(_host, _port, _log):
        return next(opened)

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= stop_after:
            raise _Stop

    monkeypatch.setattr(PumpConnection, "open", fake_open)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        run(drv.server_run())
    return drv, sleeps


class TestPumpDriverReconnect:
    def test_silent_pump_is_closed_and_reopened_with_init(self, monkeypatch):
        conns = [FakeConn(fail_after=0), FakeConn(fail_after=0)]
        drv, _ = serve(monkeypatch, conns, stop_after=2)
        for conn in conns:
            assert conn.closed == 1
            assert conn.cleared == 1  # init ran first on every connection
            assert conn.writes == [(CMD_PANEL, [0xFF])]
        # the TimeoutError message, not OSError's "cannot reach"
        errors = [c.args[0] for c in drv.mock_log.error.call_args_list]
        assert errors == [f"no reply from pump within {intelliflo.REPLY_TIMEOUT}s"] * 2

    def test_failure_messages(self):
        drv, _ = make_driver()
        msg = drv._failure_message
        assert msg(ConnectionRefusedError()) == "gateway refused the connection"
        assert (
            msg(SyntaxError("Invalid packet header"))
            == "lost packet framing: Invalid packet header"
        )
        assert (
            msg(OSError("Network unreachable"))
            == "cannot reach host:1234: Network unreachable"
        )
        assert msg(RuntimeError("boom")) == "server loop failed: RuntimeError: boom"

    def test_outage_is_pushed_to_the_app(self, monkeypatch):
        conns = [FakeConn(fail_after=0), FakeConn(fail_after=0)]
        drv, _ = serve(monkeypatch, conns, stop_after=2)
        assert drv._state.connected is False
        assert drv.updates == [True, True]  # one forced update per lost connection
        phases = [
            c.args[0] for c in drv.mock_log.info.call_args_list if "->" in c.args[0]
        ]
        assert phases == [
            "pump None -> disconnected: rpm 0 watts 0 " + drv._state.status_str()
        ]

    def test_stale_commands_are_dropped_on_connect(self, monkeypatch):
        drv, _ = make_driver()
        for _ in range(3):
            drv._enqueue(Cmd.HEARTBEAT)
        conns = [FakeConn(fail_after=0)]
        serve(monkeypatch, conns, stop_after=1, drv=drv)
        assert drain(drv._dqueue) == []

    def test_backoff_doubles_to_cap_while_silent(self, monkeypatch):
        conns = [FakeConn(fail_after=0) for _ in range(9)]
        _, sleeps = serve(monkeypatch, conns, stop_after=9)
        assert sleeps == [5, 10, 20, 40, 80, 160, 320, 600, 600]

    def test_backoff_resets_once_the_pump_answers(self, monkeypatch):
        conns = [FakeConn(fail_after=0), FakeConn(fail_after=1)]
        _, sleeps = serve(monkeypatch, conns, stop_after=2)
        assert sleeps == [5, 5]

    def test_cancel_exits_without_waiting_to_reopen(self, monkeypatch):
        conn = FakeConn()  # answers everything, so the server parks on the queue
        drv, st = make_driver()

        async def fake_open(_host, _port, _log):
            return conn

        monkeypatch.setattr(PumpConnection, "open", fake_open)

        async def go():
            task = asyncio.create_task(drv.server_run())
            for _ in range(10):
                await asyncio.sleep(0)
            assert conn.reads > 0 and not task.done()
            assert st.connected
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            return task in done and task.cancelled()

        assert run(go())
        assert conn.closed == 1
        assert not st.connected
