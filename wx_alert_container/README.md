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

### 3. Preview messages without transmitting

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

### 4. Transmit one test message

This connects, reports the node name and channel, sends one message, and
exits:

```bash
docker compose run --rm wx-alert --meshcore-test
```

```text
INFO MeshCore connected port=/dev/meshcore node='WX-Reno' channel=0
     name='Public' text_budget=141 bytes
INFO MeshCore test transmitted channel=0 bytes=57
```

If the channel index is wrong or the radio does not answer, this fails here
rather than silently doing the wrong thing for a week.

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
