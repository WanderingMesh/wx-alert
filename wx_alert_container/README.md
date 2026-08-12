# wx-alert

Monitors National Weather Service active alerts for a single point and
republishes each new or updated alert to one or more transports.

| Transport | Delivery | Use |
| --- | --- | --- |
| `ntfy` | HTTPS push | Phones and desktops while the internet is up |
| `meshcore` | LoRa broadcast via USB companion radio | Off-grid delivery |

Either or both can be enabled. They deliver independently: a radio failure
never suppresses a push notification, and vice versa.

---

## Quick start

```bash
cp .env.example .env          # set MESHCORE_DEVICE and its group ID
cp config.ini config.local.ini # add your real ntfy topic and token
$EDITOR .env config.local.ini
docker compose up -d --build
docker compose logs -f
```

Before going on the air, see [Bringing up the radio](#bringing-up-the-radio).

---

## Configuration

All settings live in `config.ini`, which is documented inline. Every setting
can be overridden per run on the command line; run `--help` for the full list.

The tracked `config.ini` is a **template and must not contain credentials**.
An ntfy topic name is a bearer secret: anyone who knows it can both read your
alerts and publish to your topic. Keep real values in `config.local.ini`,
which is gitignored and bind-mounted over the baked-in file at runtime.

`config.local.ini` must be readable by UID 10001, the unprivileged user the
container runs as. A bind mount carries host ownership through unchanged, so a
file created with a restrictive umask fails at startup with
`Permission denied: '/app/config.ini'`. Either make it world-readable:

```bash
chmod 644 config.local.ini
```

or, to keep it off-limits to other local users, give it to the container's
group and grant read access to that group alone:

```bash
sudo chgrp 10001 config.local.ini && chmod 640 config.local.ini
```

The second form needs root, because your login account is not a member of a
group that only exists inside the image.

Minimum viable configuration:

```ini
[weather]
DEFAULT_LATITUDE = 39.5296
DEFAULT_LONGITUDE = -119.8138

[ntfy]
TOPIC = your-long-unguessable-topic

[delivery]
CHECK_INTERVAL = 300
```

### Which alerts get sent where

The two transports deliberately have different postures, because the cost of
an unnecessary message is wildly different.

ntfy receives everything, with the priority mapped from the alert. MeshCore
receives only what clears `MIN_CLASS` and `MIN_SEVERITY`, which default to
warnings at severe or above.

They also differ in how much you can trust them. **ntfy is the reliable path**:
it runs over TCP, the server acknowledges each publish, and a failure is
visible and retried. **The radio is best effort**: channel messages are
unacknowledged broadcasts that can vanish without a trace. Treat the mesh as a
bonus that reaches people without internet, not as the channel you rely on to
know a warning was received.

The two are fully independent. A radio failure never suppresses or delays an
ntfy notification, and delivery is tracked per transport, so a radio that is
failing and retrying does not re-send anything on ntfy.

Alerts are classified by product name rather than by CAP severity, because
severity alone is misleading. NWS routinely issues a Flood Watch with
`severity=Severe`, and a severity-only mapping would announce it exactly as
loudly as an active Flash Flood Warning. Severity is used only to break ties
within a class.

| Product class | Example | ntfy priority (auto) | On the radio by default |
| --- | --- | --- | --- |
| warning | Flash Flood Warning | `max` / `high` | Yes |
| watch | Flood Watch | `high` / `default` | No |
| advisory | Wind Advisory | `default` | No |
| statement | Special Weather Statement | `low` | No |
| outlook | Hazardous Weather Outlook | `low` | No |

---

## Bringing up the radio

### 1. Find a stable device path

```bash
ls -l /dev/serial/by-id/
```

Use the `by-id` path, not `/dev/ttyACM0` or `/dev/ttyUSB0`. USB enumeration
order changes across reboots, and a bare device node will eventually point at
a different device.

### 2. Find the device's group

The container runs as the unprivileged `wxalert` user (UID 10001), and serial
devices are usually `root:dialout` mode 660, so the process needs the device's
group to open it:

```bash
stat -c '%g %G' "$(readlink -f /dev/serial/by-id/YOUR-RADIO)"
```

Put both values in `.env`. Granting one supplementary group is much better
than the `privileged: true` this problem usually attracts.

### 3. Confirm which channel you are about to use

`CHANNEL_INDEX` refers to a channel slot configured on the radio itself, not
in this project. The mapping from index to name is whatever you set in the
MeshCore app, so an index alone tells you nothing about where traffic lands.

**Index 0 is conventionally `Public`, which is almost never where an automated
alert feed belongs.** Put alerts on a dedicated channel and confirm the name
before enabling the loop. The self-test in step 5 resolves and logs the name:

```text
INFO MeshCore connected port=/dev/meshcore node='LNM-WXA' channel=1
     name='#rno-wx-alerts' text_budget=141 bytes
```

If that name is not the channel you intended, fix `CHANNEL_INDEX` before going
any further.

### 4. Preview messages without transmitting

`--meshcore-dry-run` renders every message and logs it with its byte size,
without opening the serial port. Review the formatting before you occupy a
shared channel with it:

```bash
docker compose run --rm wx-alert \
  --once --meshcore --meshcore-dry-run --startup-max-age 0
```

```text
INFO MeshCore dry-run channel=0 bytes=124/141 text='Flash Flood Warning: Washoe
County, til Thu 21:00. Move to higher ground now. Avoid flooded roadways.'
```

### 5. Transmit one test message

This connects, reports the node name and channel, sends one message, and
exits:

```bash
docker compose run --rm wx-alert --meshcore-test
```

```text
INFO MeshCore connected port=/dev/meshcore node='WX-Reno' channel=1
     name='#rno-wx-alerts' text_budget=141 bytes
INFO MeshCore test transmitted channel=1 bytes=57
```

Check the reported channel name, then confirm on a second node that the
message actually arrived. If the channel index is wrong or the radio does not
answer, this fails here rather than silently doing the wrong thing for a week.

---

## Message format on the radio

MeshCore caps channel message plaintext at 160 bytes, and most of that is not
available. The firmware adds a four-byte timestamp, a flags byte, a
terminator, and prepends the sending node's name plus `": "`. A node named
`WX-Reno` leaves about 141 usable bytes; a 32-character name leaves 116.

The budget is read from the radio at startup rather than hardcoded, so
renaming a node cannot silently start truncating messages.

Segments are added in descending order of value and dropped from the bottom
when they do not fit, so running out of room costs the least important
information:

```text
Flood Watch: Greater Reno-Carson City-Minden Area, til Thu 21:00. Flash
flooding caused by excessive rainfall continues to be possible.
└── event ──┘  └────── area ──────┘  └── expiry ──┘  └──── detail ────┘
   required      dropped 2nd          dropped 1st      truncated first
```

Notes on the details:

- Expiry is shown in the **alert area's local time**, which is what NWS
  supplies and what a reader needs. The weekday appears only when the end is
  not today.
- Detail text prefers the alert's `instruction`, falling back to the `WHAT`
  section of the description. The headline is never used: it restates the
  event name and times already in the message.
- A long list of counties collapses to `Washoe County +3`.
- Text is folded to ASCII, and truncation never splits a UTF-8 character.

---

## Airtime governance

Every MeshCore channel message is flood-routed and rebroadcast by every
repeater in range, so it occupies the channel for everyone nearby. Three
independent gates sit in front of the radio.

**Relevance.** `MIN_CLASS` and `MIN_SEVERITY` must both be satisfied.

**Rate limiting.** `MIN_SECONDS_BETWEEN_SENDS` and `MAX_SENDS_PER_HOUR` bound
consumption regardless of relevance. Alerts over the cap are refused and
logged, not queued: a weather alert delivered forty minutes late is worse
than useless.

**Startup staleness.** The NWS API returns every *currently active* alert, not
only newly issued ones, so a restart is indistinguishable from a burst of new
alerts. On the first cycle, products older than `STARTUP_MAX_AGE_SECONDS` are
recorded as suppressed rather than announced.

On ntfy, active warnings bypass the staleness limit, because silently
swallowing an ongoing warning is worse than a duplicate notification. On the
radio they do not, because a duplicate push costs nothing while replaying
hours-old warnings onto a shared channel after every restart costs everyone
airtime.

> A channel message is an unacknowledged broadcast. A successful send means
> the frame was accepted for transmission by the radio. Nothing in this
> program can confirm that anyone received it, which is why logs say
> "transmitted" rather than "delivered".

### Repeated transmission

Because nothing is acknowledged, a lost message is lost silently. There is no
delivery report to react to and no failure to retry. During bring-up on a busy
channel, one of four test messages simply never arrived.

The only available defence is to send each message more than once, so
`REPEAT_SENDS` defaults to 2. This is a real trade-off and not a free win:
everyone on the channel sees each alert twice, and airtime doubles.
`MAX_SENDS_PER_HOUR` counts *alerts*, not transmissions, so the default of 12
alerts per hour is up to 24 transmissions. Set `REPEAT_SENDS = 1` to disable.

Each copy ends with a `1/2`, `2/2` marker, for two reasons.

The first is plain legibility. A reader seeing the same warning twice has no
way to tell whether two separate things happened; the marker answers that
without them having to think about it.

The second is defensive, against deduplication. MeshCore deduplicates at two
levels, and neither is a reason to expect identical copies to survive:

- **Mesh layer.** `MeshTables::hasSeen()` keeps a cyclic table of SHA-256
  hashes over the payload, and repeaters use it to stop flood packets looping.
  This one should *not* affect repeats: a channel message encrypts the sender
  timestamp inside the payload, and copies at least a second apart therefore
  hash differently.
- **Client layer.** The companion protocol documentation tells client authors
  to deduplicate incoming messages, suggesting timestamp and content as the
  key. What a given client actually keys on is up to that client, and a client
  keying on content alone would silently swallow every repeat.

The marker makes the repeat robust against the second case without needing to
know which client anyone is running. See *Repeats and deduplication* under
**Open questions** for what has and has not been measured here.

The marker costs four bytes, and they are reserved from the message budget
before the text is rendered rather than trimmed afterwards, so a full-length
alert cannot overflow the firmware's limit once the marker is appended. With
`REPEAT_SENDS = 1` there is nothing to disambiguate, so no marker is added and
the full budget goes to the alert.

The gap between copies is randomised between `REPEAT_MIN_DELAY` and
`REPEAT_MAX_DELAY` rather than fixed. A constant gap can phase-lock with
another periodic sender, so two transmissions that collide once would collide
again on every repeat; jitter decorrelates them.

`REPEAT_MIN_DELAY` may not be less than one second, and startup fails if it
is. The firmware stamps each message with the current epoch second, and
repeaters discard flood packets whose hash they have already forwarded. A
sub-second gap risks two copies sharing a timestamp, which the mesh would drop
before the differing markers could save them.

A repeat that fails is logged but does not fail the delivery. The alert
already went out once, and reporting failure would requeue it and retransmit
the entire burst on the next cycle — producing more duplicates, not fewer.

---

## Persistent state

`/data/notified-alerts.json` records what has been delivered, per transport.
Mount it as a volume, as `docker-compose.yml` does. Without it, every restart
re-announces every active alert — noise over HTTPS, and a burst of
flood-routed traffic on LoRa.

Recording is per transport because they fail independently: an ntfy success
paired with a radio failure needs a representation, or the next cycle either
duplicates the notification or permanently skips the broadcast.

An alert is considered handled only when its **content fingerprint** matches.
NWS reissues products under the original ID when details change, so comparing
IDs alone would silently swallow updates.

State from an earlier single-transport build is migrated automatically on
first read. Its history is attributed to ntfy, and the radio is treated as
never having seen those alerts, so the startup staleness policy applies
rather than replaying a backlog on air.

---

## Health and recovery

The poll loop writes a heartbeat after each cycle, and `HEALTHCHECK` reads it.
The container reports unhealthy when the loop has wedged or a transport has
been failing repeatedly.

Docker does not act on health status, so reporting alone would leave a broken
container running indefinitely. The program therefore exits non-zero after
`EXIT_AFTER_FAILED_CYCLES` consecutive failures, letting
`restart: unless-stopped` recreate it.

This is what recovers a **replugged USB radio**. When the device is unplugged
and reconnected, the container still holds the original device node, which no
longer exists; no amount of reconnecting inside the process can fix it. Only
recreating the container against the current device will.

```bash
docker inspect --format '{{.State.Health.Status}}' wx-alert
```

### A hung radio, which restarting cannot fix

There is a second failure mode, and restarting the container is useless
against it. The companion firmware can stop responding while the USB device
stays present and the serial port still opens normally. Nothing about the
device node is wrong, so a recreated container connects to the same hung radio
and fails again, forever.

This was observed in practice: the radio went silent mid-session, the port
stayed free, `/dev/serial/by-id/` was unchanged, and the kernel logged no USB
reset. Reconnecting failed repeatedly for about ten minutes.

ESP32 boards wire the USB bridge's RTS line to the chip's reset pin, which is
how esptool reboots a board without anyone touching it. Driving that line
recovers the radio in place. The transport does this automatically when a
connection attempt gets no answer, controlled by `HARD_RESET_ON_HANG`.

To do it by hand, including over SSH on a headless host:

```bash
docker compose run --rm wx-alert --meshcore-reset
docker compose run --rm wx-alert --meshcore-test   # confirm it came back
```

The reset boots the application, not the ROM bootloader: DTR is held
deasserted so GPIO0 stays high while only RTS is pulsed. Reversing that would
strand the radio in the bootloader, where it answers nothing until someone
physically power-cycles it.

### If the radio hangs every time it transmits, check transmit power first

**Tentatively specific to the Heltec WiFi LoRa 32 V3.** This was seen on one
board and has not been reproduced anywhere else, so treat it as a property of
that hardware — possibly of that individual unit — rather than of MeshCore or
of this program. If you hit it on something else, the note below is wrong and
worth correcting.

The hang was caused by running the transmitter at its maximum rated power. At
`tx_power = 22`, the maximum the board reports, a single channel message hung
the firmware every time: the send returned `OK`, and seconds later the radio
stopped answering even a battery query on the still-open connection. Dropping
to **20** eliminated it — four consecutive transmissions with no fault, where
22 had failed on the first. 20 dBm has been the operating setting since, with
no recurrence.

Two decibels is a small change on the air and a large one for the power
amplifier, which draws its peak current at full output. The board was powered
over USB with a charged LiPo attached, and that did not prevent it, so do not
rule this out just because the radio has a battery.

The symptom is distinctive, and it looks nothing like a power problem:

- the send returns `OK`, so the message may well go out
- the USB device stays present and `/dev/serial/by-id/` does not change
- the kernel logs no USB reset, because the USB-serial bridge is a separate
  chip and never lost power
- only the reset pin brings it back

If this is happening, lower `tx_power` on the radio before suspecting
this program, the serial library, or the cable:

```python
await mc.commands.set_tx_power(20)   # persists across reboots
```

---

## Open questions

Things believed but not established. Recorded here so nobody mistakes them for
findings, and so anyone with the hardware to settle one knows it is worth
doing.

### Repeats and deduplication

Whether identical copies of a channel message actually get deduplicated is
**unresolved**, and the `1/2` marker described under *Repeated transmission*
is a precaution rather than a proven fix.

Two trials so far, both on `#rno-wx-alerts`:

| Trial | Sent | Arrived |
|---|---|---|
| Paired A/B test | 2 byte-identical, 2 marked | 1 of the identical, 2 of the marked |
| `--meshcore-test` self-test | 2 marked | 2 |

So marked copies are 4 for 4, and the only copy ever lost was one of an
identical pair. That is consistent with deduplication, but it does not
demonstrate it. An ordinary collision is exactly the loss the repeat exists to
cover, one message out of four had already gone missing during bring-up, and a
single missing message is a sample of one however many marked copies arrive
alongside it.

The mechanisms argue *against* deduplication being the cause, and the obvious
candidate explanation does not survive a look at the source.

That candidate is a coarse clock: if the timestamp inside the hash were
rounded to, say, the minute, copies seconds apart would hash identically and
the mesh would drop the repeat as a designed behaviour. It is not rounded.
`BaseChatMesh::sendGroupMessage()` copies the full 32-bit seconds value into
the payload with `memcpy(temp, &timestamp, 4)`, commented *"mostly an extra
blob to help make packet_hash unique"* — making the hash differ is the whole
point of it being there. The value comes from the host, which stamps
`int(time.time())` per send, and the companion protocol carries it as
seconds. Nothing in that path quantises.

So copies a second or more apart hash differently and repeaters should forward
both. The companion protocol suggests clients key on timestamp and content
together, which would also pass both. Deduplication would only explain the
result if some client in the path keys on content alone.

To settle it, run the paired test enough times to count identical-pair
arrivals. Identical copies arriving *never* is deduplication; arriving
sometimes is loss. One trial cannot tell those apart, and the marked copies
say nothing either way — they are the control.

The marker is worth keeping either way: it costs four bytes, and it tells a
reader that they are looking at one warning rather than two.

---

## Container image

Built from `python:3.13-slim-trixie` in two stages, so the pip toolchain used
to create the virtualenv stays out of the runtime image. No apt packages are
installed: every dependency ships prebuilt manylinux wheels, so no compiler
or extra shared library is needed.

The base tag is pinned to the Debian codename deliberately. Plain `slim` would
follow Debian to its next stable release and change the OS under the
application; `3.13-slim-trixie` still picks up CPython patch releases and
Debian security updates when you rebuild.

The process runs as `wxalert`, UID **10001**, with no home directory and no
login shell. The UID is fixed rather than auto-assigned because it owns the
`/data` volume — a UID that drifted between rebuilds would leave the container
unable to read back its own history, and it would re-announce every active
alert.

> If you are upgrading from a build that used a different UID, either
> `chown -R 10001:10001` the volume contents or start from a fresh volume and
> accept one round of duplicate notifications.

The compose file additionally runs the container read-only, drops all
capabilities, and sets `no-new-privileges`. The process needs none of them: it
opens a serial device it has group access to, and makes outbound HTTPS
requests. It writes only to `/data`.

---

## Common operations

```bash
# One check, then exit
docker compose run --rm wx-alert --ntfy --once

# Test ntfy without querying NWS
docker compose run --rm wx-alert --ntfy-test

# Test both transports in one run. Each is reported separately, so a radio
# failure still shows whether ntfy worked.
docker compose run --rm wx-alert --ntfy-test --meshcore-test

# Reboot a hung radio, then confirm it answers again
docker compose run --rm wx-alert --meshcore-reset
docker compose run --rm wx-alert --meshcore-test

# Transmit each message once instead of twice, for this run only
docker compose run --rm wx-alert --meshcore --once --meshcore-repeat 1

# Full alert text to stdout and to ntfy
docker compose run --rm wx-alert --ntfy --loop --verbose

# Preview radio output for one cycle without transmitting
docker compose run --rm wx-alert \
  --meshcore --meshcore-dry-run --once --startup-max-age 0

# Inspect delivery history
docker compose exec wx-alert python -m json.tool /data/notified-alerts.json

# Stop cleanly (SIGTERM is handled; it will not wait out the poll interval)
docker compose down
```

An explicit command replaces the Dockerfile `CMD`, so include the transport
flags you want each time.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Clean exit, or a successful one-shot check |
| 1 | NWS query failed |
| 2 | One or more deliveries failed |
| 3 | Configuration or persistent state could not be initialized |
| 4 | A transport failed repeatedly; exited so it can be recreated |

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
```

The suite needs no network and no radio; MeshCore is exercised against a fake
companion radio.

```text
wx_alert/
  __main__.py     entry point, builds transports from configuration
  app.py          poll loop, delivery coordination, heartbeat
  cli.py          argument parsing layered over config.ini
  config.py       configuration loading and validation
  nws.py          NWS API access, classification, fingerprints
  state.py        persistent per-transport delivery history
  policy.py       startup staleness policy
  ratelimit.py    relevance filter and airtime rate limiter
  formatting.py   stdout and ntfy rendering
  mesh_format.py  byte-budgeted rendering for LoRa
  radio.py        serial hard reset for a hung companion radio
  health.py       heartbeat and HEALTHCHECK entry point
  transports/     base protocol, ntfy, meshcore
```

Adding a transport means implementing `Transport` in
`wx_alert/transports/base.py` and constructing it in `build_transports`.
Delivery returns one of three outcomes, and the distinction between the last
two is what keeps state coherent:

- `SENT` — handed off successfully.
- `SKIPPED` — a deliberate decision not to send. Recorded as settled so it is
  never retried.
- `FAILED` — an accident. Left unsettled so the next cycle retries it.
