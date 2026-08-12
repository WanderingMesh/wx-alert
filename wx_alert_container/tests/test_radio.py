"""The serial hard reset.

The ordering of the control lines is the whole point of these tests. Asserting
DTR during the reset pulse would drive GPIO0 low and drop the chip into the ROM
bootloader, where it answers nothing and stays until someone physically
unplugs it. That is strictly worse than the hang being recovered from, and it
is a one-character mistake, so it is pinned here.
"""

from __future__ import annotations

import pytest

from wx_alert.radio import RadioResetError, hard_reset


class RecordingSerial:
    """A stand-in for pyserial that records control-line transitions."""

    def __init__(self, port):
        object.__setattr__(self, "events", [])
        self.port = port
        self.closed = False

    def __setattr__(self, name, value):
        if name in {"dtr", "rts"}:
            self.events.append((name, value))
        super().__setattr__(name, value)

    def close(self):
        self.events.append(("close", None))
        super().__setattr__("closed", True)


@pytest.fixture
def reset_call():
    """Run hard_reset against a fake port, capturing lines and waits."""
    opened = []
    waits = []

    def run(**overrides):
        def factory(port):
            connection = RecordingSerial(port)
            opened.append(connection)
            return connection

        settings = {
            "hold": 0.15,
            "settle": 3.0,
            "sleep": waits.append,
            "serial_factory": factory,
        }
        settings.update(overrides)
        hard_reset("/dev/fake", **settings)
        return opened[-1], waits

    return run


class TestHardReset:
    def test_never_asserts_dtr(self, reset_call):
        # DTR high would mean GPIO0 low: the ROM bootloader, not the app.
        connection, _ = reset_call()
        assert ("dtr", True) not in connection.events

    def test_pulses_rts_low_then_high(self, reset_call):
        connection, _ = reset_call()
        rts = [value for name, value in connection.events if name == "rts"]
        assert rts == [True, False]

    def test_deasserts_dtr_before_pulsing_reset(self, reset_call):
        connection, _ = reset_call()
        names = [name for name, _ in connection.events]
        assert names.index("dtr") < names.index("rts")

    def test_closes_the_port(self, reset_call):
        connection, _ = reset_call()
        assert connection.closed

    def test_waits_for_boot_after_closing(self, reset_call):
        # Some bridges toggle the control lines again on close, so waiting
        # before the close would time the boot from the wrong moment.
        connection, waits = reset_call(hold=0.15, settle=3.0)
        close_index = [name for name, _ in connection.events].index("close")
        assert close_index == len(connection.events) - 1
        assert waits == [0.15, 3.0]

    def test_reports_a_port_that_cannot_be_opened(self):
        def factory(port):
            raise OSError("No such file or directory")

        with pytest.raises(RadioResetError, match="could not open"):
            hard_reset("/dev/absent", sleep=lambda _: None, serial_factory=factory)

    def test_closes_the_port_even_when_driving_the_lines_fails(self):
        class Stubborn(RecordingSerial):
            def __setattr__(self, name, value):
                if name == "rts":
                    raise OSError("device disconnected")
                super().__setattr__(name, value)

        created = []

        def factory(port):
            connection = Stubborn(port)
            created.append(connection)
            return connection

        with pytest.raises(RadioResetError, match="control lines"):
            hard_reset("/dev/fake", sleep=lambda _: None, serial_factory=factory)

        assert created[0].closed
