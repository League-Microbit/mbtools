"""Driving one relay board through its command plane.

Ported near-verbatim (sprint 004, ticket 003) from
``microbit-radio-relay/server/src/mbrelay/relay.py`` and the
``ByteChannel``/``ChannelFactory`` interface half of that repo's
``transport.py`` -- the command sequencing, retry/verification logic, and
firmware quirks below are unchanged. What *did* change crossing into
``mbtools`` is the concurrency model: the old codebase drove the channel
from an asyncio event loop (``add_reader``, ``asyncio.Event``); nothing
else in ``mbtools`` uses asyncio (``registry.store``'s own docstring: "the
store is thread-safe (internal RLock)"; ``serial.connect`` is a plain
blocking/threaded module), so this module is the synchronous, blocking
equivalent -- ``time.sleep`` for ``asyncio.sleep``, ``threading.Event``
for ``asyncio.Event``. The sequencing this module exists to preserve is
unaffected either way: nothing here depended on being awaited concurrently
with anything else.

Also changed: :meth:`RelayControl.reset_and_normalize` now also queries
``!VER?`` (between ``HELLO`` and the normalize batch) so every acquired
board's firmware version is known, matching sprint.md's Solution section
("resets and normalizes on acquire (BREAK/reset, HELLO, !VER?, RAW250/
frag-off/echo-off/P7/ch0-grp10)") -- the legacy repo's ``relay.py`` only
queried firmware version from ``probe()``, not from acquire. See that
method's own docstring for the detail.

Also changed: :meth:`RelayControl.reset_and_normalize` and
:meth:`RelayControl.probe` take an already-constructed, not-yet-open
``ByteChannel`` directly, rather than a ``(factory, port)`` pair. Ticket
004's ``LocalRelayChannel``/``RemoteRelayChannel`` are each constructed
already knowing what they open (a local port path, or a registry-obtained
remote stream) -- there is no separate "factory + port string" step for
them the way the old codebase's ``SerialChannelFactory.open(port)`` had.
``ChannelFactory`` is still ported below (the ticket's Approach section
asks for it, interface only), but nothing in this module calls it.

The firmware quirks the sequencing below encodes are load-bearing:

* **Reset means close AND reopen the channel.** An in-place DTR toggle
  does not reset the board. Since the data plane has no in-band escape,
  close/reopen is the *only* way back to the command plane -- which is
  exactly why release works.
* **The boot banner is normally missed**, because it is emitted while the
  host is still opening the port. So we always ask again with ``HELLO``.
* **``!DEFAULTS`` does not change live state.** It clears the stored flash
  record; the compiled defaults only take effect on the *next* reset.
  Normalizing therefore has to send explicit values. Do not "simplify"
  this away -- the old repo's ``test_relay_sequences.py`` pinned it, and
  this module's own tests do too.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol

log = logging.getLogger(__name__)


class RelayError(Exception):
    """The board did not behave the way the protocol says it should."""


# ---------------------------------------------------------------------------
# ByteChannel / ChannelFactory -- ported from transport.py (interface only,
# not SerialChannel, which is legacy-repo-specific and superseded by ticket
# 004's LocalRelayChannel/RemoteRelayChannel).
# ---------------------------------------------------------------------------

OnData = Callable[[bytes], None]
OnError = Callable[["BaseException | None"], None]


class ByteChannel(Protocol):
    """A duplex byte channel to one relay board."""

    def open(self) -> None:
        """Open the channel. THIS RESETS THE BOARD (the open toggles DTR)."""

    def close(self) -> None:
        ...

    def start_reading(self, on_data: OnData, on_error: OnError) -> None:
        ...

    def stop_reading(self) -> None:
        ...

    def write_nowait(self, data: bytes) -> None:
        """Queue bytes. Never blocks; buffers if the channel is not ready."""

    def send_break(self, duration: float = 0.4) -> None:
        """Assert a break condition, which reboots the board.

        The escape hatch for a board stuck in the relay's data plane on a
        platform where close/reopen does not reset it. See
        :meth:`RelayControl.hello`.
        """

    def drain(self, timeout: float = 2.0) -> None:
        ...

    @property
    def pending_bytes(self) -> int:
        ...

    def set_watermarks(self, on_high: Callable[[], None],
                        on_low: Callable[[], None]) -> None:
        ...


class ChannelFactory(Protocol):
    def open(self, port: str) -> ByteChannel:
        ...


# ---------------------------------------------------------------------------
# banner parsing -- local copy of inventory.py's BANNER_RE/IDENTITY_RE (the
# one import this ticket replaces; nothing else in inventory.py/firmware.py/
# admin.py/the mDNS advertiser is needed here).
# ---------------------------------------------------------------------------

# Tolerant across both firmware families: RADIOBRIDGE prints the serial in
# decimal, the older RADIORELAY in hex. See docs/announce.md (legacy repo).
BANNER_RE = re.compile(
    rb"DEVICE:(RADIOBRIDGE|RADIORELAY):relay:([^:]+):([0-9A-Fa-f]+)")

# Any micro:bit's identity, in either dialect:
#   DEVICE:RADIOBRIDGE:relay:getez:1784514240     relay firmware, colon form
#   device RADIORELAY relay getez 1784514240      older relay firmware, space form
IDENTITY_RE = re.compile(
    rb"DEVICE:(?P<role>\w+):(?P<common>[^:\s]+):(?P<name>[^:\s]+):(?P<serial>[0-9A-Fa-f]+)"
    rb"|\bdevice (?P<role_r>\w+) (?P<common_r>\S+) (?P<name_r>\S+) (?P<serial_r>[0-9A-Fa-f]+)")

# printConfig() output. One line confirms channel, group, mode and power at once.
CONFIG_RE = re.compile(
    rb"#\s*channel:\s*(\d+)\s+group:\s*(\d+)\s+mode:\s*(\S+)\s+power:\s*(\d+)")

#: Restore the compiled-in factory defaults, explicitly, one command at a time.
#:
#: Each entry is (command, expected acknowledgement). ``!C 0`` is last because it
#: forces group 10, so it must not be undone by a later command.
#:
#: Sent one at a time rather than as one burst. Two reasons, both learned on
#: hardware: ``!MODE`` reconfigures the radio (``applyRadioConfig`` disables the
#: peripheral and spins on hardware flags with the radio IRQ masked), and a burst
#: arriving behind that lands in a board that is not reading, which shows up as
#: ``# error: unknown command`` for the commands that got chopped.
NORMALIZE_STEPS: tuple[tuple[bytes, bytes], ...] = (
    (b"!MODE RAW250\n", rb"#\s*mode:\s*RAW250"),
    (b"!FRAG OFF\n", rb"#\s*frag:\s*OFF"),
    (b"!ECHO OFF\n", rb"#\s*echo:\s*OFF"),
    (b"!P 7\n", rb"#\s*channel:"),
    (b"!C 0\n", rb"#\s*channel:\s*0\s+group:\s*10"),
)

#: The whole batch, for callers that only want to look at the bytes (tests do).
NORMALIZE = b"".join(cmd for cmd, _ in NORMALIZE_STEPS)

#: What ``?`` must report once the steps above have been applied.
DEFAULT_CFG = (0, 10, b"RAW250", 7)

#: The reply to ``!VER?`` -- or the refusal of firmware that predates it, so an
#: old board answers in one round trip instead of costing the whole timeout.
VERSION_RE = re.compile(rb"#\s*(?:version:\s*(\S+)|error: unknown command)")
#: The robot firmware's reply to ``VER`` (pxt-nezha-diffdrive wire_handler.cpp).
ROBOT_VERSION_RE = re.compile(rb"(?m)^ver (\S+)")

#: Roles whose firmware is a relay. Anything else that answers is asked ``VER``,
#: the robot firmware's version query.
RELAY_ROLES = ("RADIOBRIDGE", "RADIORELAY")


@dataclass(frozen=True)
class BannerInfo:
    role: str
    device_name: str
    serial: str
    raw: bytes
    firmware: str = ""          # from !VER? or VER; "" when the firmware cannot say

    @classmethod
    def parse(cls, data: bytes) -> "BannerInfo | None":
        m = IDENTITY_RE.search(data)
        if not m:
            return None
        role = m.group("role") or m.group("role_r")
        name = m.group("name") or m.group("name_r")
        serial = m.group("serial") or m.group("serial_r")
        return cls(role=role.decode(), device_name=name.decode(errors="replace"),
                    serial=serial.decode(), raw=m.group(0))


class Reader:
    """Accumulates everything a channel emits so sequences can scan for patterns."""

    def __init__(self, channel: ByteChannel) -> None:
        self.buf = bytearray()
        self._event = threading.Event()
        self.error: "BaseException | None" = None
        self.failed = False
        channel.start_reading(self._on_data, self._on_error)

    def _on_data(self, data: bytes) -> None:
        self.buf.extend(data)
        self._event.set()

    def _on_error(self, exc: "BaseException | None") -> None:
        self.error, self.failed = exc, True
        self._event.set()

    def clear(self) -> None:
        self.buf.clear()

    def complete_lines(self) -> bytes:
        """The buffer up to and including its last newline.

        Everything the relay says in the command plane is a whole line, and
        matching a pattern against a PARTIAL line is how banners came back
        truncated in the field: BANNER_RE ends in ([0-9A-Fa-f]+), so the moment a
        read delivered ":getez:1" it matched with a one-digit serial and the
        remaining digits were discarded. Only ever match what has terminated.
        """
        end = self.buf.rfind(b"\n")
        return bytes(self.buf[:end + 1]) if end >= 0 else b""

    def wait_for(self, pattern: "re.Pattern[bytes]", timeout: float):
        """Scan the completed lines for a pattern until it matches or time runs out."""
        deadline = time.monotonic() + timeout
        while True:
            if m := pattern.search(self.complete_lines()):
                return m
            if self.failed:
                raise RelayError(f"device read failed: {self.error!r}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._event.clear()
            if self._event.wait(timeout=remaining):
                continue
            return pattern.search(self.complete_lines())


class RelayControl:
    """Command-plane sequences, shared by acquire and release.

    Both paths need the identical "reset the board and put it back to factory
    defaults" dance, so it lives here once.

    Timing defaults mirror the legacy repo's ``SerialConfig`` defaults
    (``config.py``, not ported here -- no config module is planned for
    ``mbtools``'s relay client per sprint.md's Step 3 module table, so
    ``RelayControl`` takes its timing directly rather than through a
    ``cfg.serial`` object).
    """

    def __init__(self, *, open_settle: float = 0.3, hello_timeout: float = 2.0,
                 hello_attempts: int = 4, post_close_settle: float = 0.5,
                 break_duration: float = 0.4, break_settle: float = 1.2) -> None:
        self.open_settle = open_settle
        self.hello_timeout = hello_timeout
        self.hello_attempts = max(1, hello_attempts)
        self.post_close_settle = post_close_settle
        self.break_duration = break_duration
        self.break_settle = break_settle

    def hello(self, channel: ByteChannel, reader: Reader,
              allow_break: bool = True, pattern: "re.Pattern" = BANNER_RE) -> BannerInfo:
        """Confirm we are in the command plane, and learn which board this is.

        ``pattern`` is the relay banner for acquire, which can only drive a
        relay; a probe passes IDENTITY_RE so any board that answers is named.

        The boot banner went out while we were still opening the port, so ask
        again. Several attempts, because a board still running its boot animation
        can miss the first line.

        If nothing answers, send a break before giving up. A silent board is
        almost always one sitting in the data plane, where "HELLO" is radio
        payload rather than a command -- and the data plane has no in-band
        escape, so the only way out is a reboot.

        Reopening the port is supposed to be that reboot, and on macOS it is. On
        Linux it is not: measured on Ubuntu 24.04 against DAPLink v0257, neither
        close/reopen, nor a DTR pulse, nor a 1200-baud touch resets the target,
        and a board parked in the data plane stays deaf indefinitely. A break
        condition rescues it every time. So the break is not paranoia -- without
        it the release guarantee simply does not hold on the platform the fleet
        actually runs.
        """
        if info := self._ask_hello(channel, reader, pattern):
            return info

        if allow_break:
            log.info("no banner; sending a break to reset the board "
                      "(it is probably stuck in the data plane)")
            try:
                channel.send_break(self.break_duration)
            except Exception as exc:
                log.debug("send_break failed: %r", exc)
            else:
                time.sleep(self.break_settle)
                reader.clear()
                if info := self._ask_hello(channel, reader, pattern):
                    log.info("break recovered the board")
                    return info

        raise RelayError("no DEVICE banner after "
                          f"{self.hello_attempts} HELLO attempts"
                          f"{' and a break' if allow_break else ''} -- "
                          "board may not be running relay firmware")

    def _ask_hello(self, channel: ByteChannel, reader: Reader,
                    pattern: "re.Pattern" = BANNER_RE) -> "BannerInfo | None":
        for attempt in range(self.hello_attempts):
            channel.write_nowait(b"HELLO\n")
            match = reader.wait_for(pattern, self.hello_timeout)
            if match:
                info = BannerInfo.parse(match.group(0))
                if info is not None:
                    return info
            log.debug("no banner yet attempt=%d/%d", attempt + 1, self.hello_attempts)
        return None

    def normalize(self, channel: ByteChannel, reader: Reader,
                  step_timeout: float = 1.5, retries: int = 1) -> None:
        """Force the board back to factory defaults, then verify with a fresh query.

        Verification is a separate ``?`` rather than a scan of the replies to the
        setting commands. That is not belt-and-braces, it is necessary: ``!P``
        *also* calls printConfig(), so the batch emits more than one config line
        and the earliest one still shows the channel we are about to change. A
        standalone ``?`` produces exactly one line, describing the state that
        actually ended up on the board.
        """
        for attempt in range(retries + 1):
            for command, ack in NORMALIZE_STEPS:
                reader.clear()
                channel.write_nowait(command)
                # A missed ack is not fatal -- the query below is the authority.
                reader.wait_for(re.compile(ack), step_timeout)

            config = self.query(channel, reader, timeout=step_timeout * 2)
            if config is not None and self._is_default(config):
                return
            log.debug("normalize not confirmed attempt=%d config=%r tail=%r",
                      attempt + 1,
                      config.group(0) if config else None,
                      bytes(reader.buf)[-120:])
            # Let the board finish whatever it is still emitting before trying
            # again, so the retry is not written into a board mid-reply.
            time.sleep(0.4)
        raise RelayError("board did not confirm factory defaults after normalize; "
                          f"last output: {bytes(reader.buf)[-160:]!r}")

    def firmware_version(self, channel: ByteChannel, reader: Reader,
                         timeout: float = 1.0) -> str:
        """The relay firmware's build version, or "" for firmware older than
        ``!VER?`` -- which answers ``# error: unknown command`` and is otherwise
        perfectly serviceable, so this is never a reason to refuse a board."""
        reader.clear()
        channel.write_nowait(b"!VER?\n")
        match = reader.wait_for(VERSION_RE, timeout)
        return match.group(1).decode(errors="replace") if match and match.group(1) else ""

    def robot_version(self, channel: ByteChannel, reader: Reader,
                      timeout: float = 1.0) -> str:
        """A robot's firmware version, from its ``VER`` (``ver <version>``), or
        "" when nothing answers -- a board we cannot name the build of is still
        a board we can name."""
        reader.clear()
        channel.write_nowait(b"VER\n")
        match = reader.wait_for(ROBOT_VERSION_RE, timeout)
        return match.group(1).decode(errors="replace") if match else ""

    def query(self, channel: ByteChannel, reader: Reader,
             timeout: float = 3.0) -> "re.Match[bytes] | None":
        """Ask the board what its live config is. One command, one reply line."""
        reader.clear()
        channel.write_nowait(b"?\n")
        return reader.wait_for(CONFIG_RE, timeout)

    @staticmethod
    def _is_default(match: "re.Match[bytes]") -> bool:
        channel, group, mode, power = match.groups()
        return (int(channel), int(group), mode, int(power)) == DEFAULT_CFG

    def clear_stored_config(self, channel: ByteChannel, reader: Reader,
                            timeout: float = 1.0) -> None:
        """Drop the saved flash record so a *cold* boot also lands on defaults.

        Best-effort: normalize() has already fixed the live state, and this only
        matters if the board is power-cycled before its next session.
        """
        reader.clear()
        channel.write_nowait(b"!DEFAULTS\n")
        reader.wait_for(re.compile(rb"#\s*stored config cleared"), timeout)

    def reset_and_normalize(self, channel: ByteChannel, clear_stored: bool = True
                            ) -> BannerInfo:
        """Open the channel (which resets the board), verify, and restore defaults.

        Used by acquire (to guarantee a clean board), by release (to leave one),
        and by ``mbrelay reset``. Sequence: HELLO, ``!VER?``, then the
        ``NORMALIZE_STEPS`` batch verified by a standalone ``?`` (sprint.md's
        Solution section: "resets and normalizes on acquire (BREAK/reset,
        HELLO, !VER?, RAW250/frag-off/echo-off/P7/ch0-grp10)") -- unlike the
        legacy repo's ``relay.py``, where ``reset_and_normalize`` did not
        query the firmware version and only ``probe()`` did, this folds that
        query in so every acquired board's ``BannerInfo.firmware`` is
        populated, not just a probed one.
        """
        channel.open()
        try:
            time.sleep(self.open_settle)
            reader = Reader(channel)
            info = self.hello(channel, reader)
            info = replace(info, firmware=self.firmware_version(channel, reader))
            self.normalize(channel, reader)
            if clear_stored:
                self.clear_stored_config(channel, reader)
            return info
        finally:
            channel.close()

    def probe(self, channel: ByteChannel) -> "BannerInfo | None":
        """Identify a board: its name, role and firmware version, whatever it runs.

        Returns None if nothing answers -- not an error. A blank board simply
        stays quiet; the inventory backs off rather than rebooting it every scan.
        A robot answers in its own dialect and is named like any relay.
        """
        channel.open()
        try:
            time.sleep(self.open_settle)
            reader = Reader(channel)
            try:
                info = self.hello(channel, reader, pattern=IDENTITY_RE)
            except RelayError:
                return None
            if info.role == "RADIOBRIDGE":
                return replace(info, firmware=self.firmware_version(channel, reader))
            if info.role in RELAY_ROLES:
                return info                 # the older relay family has no version query
            return replace(info, firmware=self.robot_version(channel, reader))
        finally:
            channel.close()
