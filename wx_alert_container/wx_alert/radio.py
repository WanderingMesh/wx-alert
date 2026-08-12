"""Reboot a MeshCore companion radio over the serial control lines.

The companion firmware can stop answering while the USB device stays present
and the serial port stays openable. That failure is invisible to Docker: the
device node is fine, so restarting the container reconnects to the same hung
radio and fails again, forever.

ESP32 boards with a USB-UART bridge wire the bridge's RTS line to the chip's
EN pin and DTR to GPIO0, which is how esptool and the Arduino IDE reset a
board without anyone touching it. Driving those lines deliberately gives this
program a way out of a hang that a container restart cannot fix.

The sequence below is a plain reset into the application, not into the ROM
bootloader. Only RTS is pulsed; DTR is held deasserted so GPIO0 stays high and
the chip boots normally. Getting that backwards would strand the radio in the
bootloader until someone physically power-cycled it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger("wx-alert")

# Long enough for the EN pin to register a reset, short enough to be invisible.
DEFAULT_HOLD_SECONDS = 0.15

# The firmware has to boot and complete its own radio initialization before it
# will answer the companion handshake. Reconnecting sooner just fails.
DEFAULT_SETTLE_SECONDS = 3.0


class RadioResetError(RuntimeError):
    """Raised when the serial control lines could not be driven."""


def _open_serial(port: str) -> Any:
    """Open the port with pyserial.

    Imported lazily so that dry runs and unit tests never need the dependency,
    and so a missing pyserial surfaces here rather than at program start.
    """
    import serial

    return serial.Serial(port)


def hard_reset(
    port: str,
    *,
    hold: float = DEFAULT_HOLD_SECONDS,
    settle: float = DEFAULT_SETTLE_SECONDS,
    sleep: Callable[[float], Any] = time.sleep,
    serial_factory: Callable[[str], Any] = _open_serial,
) -> None:
    """Pulse EN low and back, then wait for the radio to finish booting.

    `sleep` is injected so callers can wait on the shutdown event instead of
    blocking, and so tests do not spend real seconds.
    """
    LOGGER.warning("Hard-resetting the MeshCore radio port=%s", port)

    try:
        connection = serial_factory(port)
    except Exception as exc:  # noqa: BLE001 - pyserial raises several types,
        # and every one of them means the same thing to the caller.
        raise RadioResetError(
            f"could not open {port} to reset the radio: {exc}"
        ) from exc

    try:
        # DTR deasserted throughout keeps GPIO0 high, so the chip boots the
        # application instead of the ROM bootloader.
        connection.dtr = False
        connection.rts = True   # EN low: held in reset
        sleep(hold)
        connection.rts = False  # EN high: boot
        connection.dtr = False
    except Exception as exc:  # noqa: BLE001 - as above
        raise RadioResetError(
            f"could not drive the control lines on {port}: {exc}"
        ) from exc
    finally:
        try:
            connection.close()
        except Exception as exc:  # noqa: BLE001 - closing failing is not worth
            # masking whatever the caller was actually trying to do.
            LOGGER.debug("Error closing %s after reset: %s", port, exc)

    # Closing the port can toggle the control lines again on some bridges, so
    # the settle wait belongs after the close rather than before it.
    sleep(settle)
    LOGGER.info("Radio reset complete; waited %.1fs for boot", settle)
