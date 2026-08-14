"""Region scoping for transmitted channel messages.

A MeshCore region is a named area whose name hashes to a 16-byte transport
key. Scoping a flood packet to a region does not filter anything at the
sender: it makes the packet forwardable only by repeaters that carry that
region. Every repeater without it drops the packet instead of relaying.

That inverts the intuition the word "scope" invites. A narrow scope does not
deliver a message to a smaller area, it delivers to whatever subset of that
area happens to be covered by correctly configured repeaters. Scope to a
region no repeater in range carries and the message reaches direct neighbours
only, with nothing forwarding it onward and no error anywhere to say so.

Two details make this easy to misconfigure, so both are handled here rather
than left to the operator:

The key is a hash of the exact string, so `RNO` and `rno` are different
regions that look identical in a config file. Case is preserved and never
folded, because guessing which case a mesh standardised on would be worse than
making the operator match it.

The underlying library also assigns meaning to several strings that look like
ordinary names. `"0"`, `"None"` and `""` all mean "revert to the device
default", and `"*"` means "force unscoped". Those are mapped explicitly below
so that a region genuinely named `0` fails loudly rather than silently doing
something else.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

# Explicitly transmit without a scope, overriding any default the operator set
# on the radio itself. Distinct from leaving SCOPE blank, which does not touch
# the radio's scope state at all.
FORCE_UNSCOPED = "*"

# The firmware stores a scope name in a 31-byte field, including the leading
# marker, so the name itself has 30 bytes to work with.
MAX_SCOPE_NAME_BYTES = 30

# Strings the meshcore library interprets rather than treats as a name.
_LIBRARY_RESERVED = ("0", "None")


def normalize_scope(raw: str | None, source: str) -> str | None:
    """Validate a configured scope, returning the value to send to the radio.

    Returns None when no scope is configured, which means the radio's own
    scope state is left alone. That is deliberately different from forcing
    unscoped: an operator who set a default scope on the device chose it, and
    silently overriding that choice would be worse than inheriting it.
    """
    if raw is None:
        return None

    value = raw.strip()

    if not value:
        return None

    if value == FORCE_UNSCOPED:
        return FORCE_UNSCOPED

    if value in _LIBRARY_RESERVED:
        raise ValueError(
            f"{source}: {value!r} is reserved by the MeshCore library to mean "
            "'revert to the radio's default scope'. Leave the setting blank "
            "to leave the radio's scope alone, or use '*' to force unscoped."
        )

    if any(character.isspace() for character in value):
        raise ValueError(f"{source}: {value!r} may not contain whitespace")

    name = value if value.startswith("#") else f"#{value}"

    if len(name) <= 1:
        raise ValueError(f"{source}: {value!r} is not a region name")

    encoded = name[1:].encode("utf-8")

    if len(encoded) > MAX_SCOPE_NAME_BYTES:
        raise ValueError(
            f"{source}: region name {name!r} is {len(encoded)} bytes, over "
            f"the {MAX_SCOPE_NAME_BYTES} the firmware stores"
        )

    return name


def scope_key(name: str) -> str:
    """The transport key a region name resolves to, hex-encoded.

    Logged at startup because the key, not the name, is what has to match the
    repeaters. Printing it turns a case or spelling mismatch from an invisible
    loss of coverage into a value an operator can compare against their
    repeater configuration.
    """
    return sha256(name.encode("utf-8")).digest()[:16].hex()


@dataclass(frozen=True)
class ProbeRun:
    """What came back from one leg of a scope probe."""

    label: str
    scope: str | None
    replied: bool
    text: str | None = None
    path_len: int | None = None

    @property
    def hops(self) -> str:
        """The reply's hop count, rendered for a report.

        A direct reply took no relay, which is worth distinguishing: a bot
        within earshot answers regardless of scope, so a zero-hop success
        proves the scope was accepted but says nothing about whether anything
        would forward it.
        """
        if self.path_len is None:
            return "unknown"
        if self.path_len == 0:
            return "direct, no relay"
        return f"{self.path_len} hop(s)"


def interpret_probe(control: ProbeRun, scoped: ProbeRun) -> tuple[bool, str]:
    """Turn two probe legs into a verdict.

    The control leg is what makes the scoped leg readable. Silence from a
    scoped probe on its own has at least three explanations — no repeater
    carries the region, the bot is down, the channel index is wrong — and
    they are indistinguishable. Establishing first that the bot answers when
    unscoped removes two of them.
    """
    if not control.replied:
        return False, (
            "Inconclusive. The unscoped control got no reply either, so this "
            "says nothing about the region. Check that the bot is up and that "
            "the probe channel index is right, then run it again."
        )

    if not scoped.replied:
        return False, (
            f"{scoped.scope} does not work here. The control was answered "
            f"({control.hops}) and the scoped probe was not, which means "
            "nothing able to reach the bot carries that region. Alerts sent "
            "under it would reach direct neighbours only."
        )

    if scoped.path_len == 0 and (control.path_len or 0) > 0:
        return False, (
            f"Weak evidence for {scoped.scope}. The reply came back direct "
            "while the control was relayed, so the bot heard the transmission "
            "itself and no repeater is known to have forwarded it. Re-run "
            "from somewhere that needs a relay to reach the bot."
        )

    return True, (
        f"{scoped.scope} works. The scoped probe was answered "
        f"({scoped.hops}), so a repeater carrying the region relayed it. "
        "Read the bot's reply below to confirm the region it saw matches."
    )
