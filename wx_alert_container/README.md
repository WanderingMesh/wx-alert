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

### Which area is monitored

Alerts are fetched by **county**, not by coordinate. This matters more than it
sounds, and getting it wrong is how a real Tornado Warning went undelivered.

Since 2007 the NWS has issued its convective and flash-flood products as
*storm-based warnings*: the forecaster draws a polygon around the threat, and
the counties listed on the warning are legacy metadata for NOAA Weather Radio
and EAS, which can only address whole counties. The polygon is the warned area.
`api.weather.gov` honours that distinction, so a `?point=` query intersects your
coordinate with the polygon and returns nothing when the storm is a few miles
away — even though your county is named on the warning. Zone products such as
watches and statements have no polygon and were always returned, which is what
made the gap so easy to miss: the log looked healthy right up until a tornado
warning simply never appeared.

Querying the county instead returns polygon warnings and zone products alike.
The county containing `DEFAULT_LATITUDE` / `DEFAULT_LONGITUDE` is resolved once
at startup through the NWS `/points` endpoint and cached in `/data`, so no
configuration is needed to get this right.

**Use county codes, never forecast zone codes.** `NVC031` is Washoe County;
`NVZ003` is the Greater Reno forecast zone. Both are valid UGC codes, both are
accepted by the API, and the second one returns no storm-based warnings at all.
It fails by going quiet.

```ini
[weather]
DEFAULT_LATITUDE = 39.5296
DEFAULT_LONGITUDE = -119.8138

# Optional. The containing county is always queried; add neighbours the mesh
# reaches into. RF coverage does not stop at a county line.
ZONES = NVC029,NVC019

# Discard warnings whose polygon is farther than this. 0 disables the test.
ALERT_RADIUS_KM = 50
```

`ALERT_RADIUS_KM` exists because counties are not a uniform unit. Washoe County
runs 315 km from Reno to the Oregon border; Arlington VA is 12 km across. A bare
county query would therefore mean something completely different depending on
where this runs, and on a shared LoRa channel the large-county case is expensive:
warnings for places no node can hear. The radius restores local relevance
without narrowing the fetch. Alerts with no polygon are always kept, since NWS
has already scoped them to the county.

If the `/points` lookup fails and nothing is cached, the program refuses to
start unless `ZONES` is set explicitly. An empty zone list would return an empty
alert list, and the container would report quiet weather indefinitely.

### Finding your county codes

You do not need any of this to get started. The county containing your
coordinates is resolved automatically, and leaving `ZONES` blank is a perfectly
good configuration. This is only for adding the *neighbouring* counties a
wide-area mesh reaches into, since a radio network's footprint pays no
attention to county lines.

**Ask about a specific place.** The most direct method: pick a coordinate in a
town your mesh actually covers and ask which county contains it.

NWS requires a `product/version (contact)` User-Agent and its edge returns a
bare `403` without one, so the version token is not decoration.

```bash
curl -s -H "User-Agent: wx-alert/1.0 (you@yourdomain.org)" \
  https://api.weather.gov/points/39.5296,-119.8138 \
  | grep -E '"(county|forecastZone)": "'
```

```json
"forecastZone": "https://api.weather.gov/zones/forecast/NVZ003",
"county": "https://api.weather.gov/zones/county/NVC031",
```

The last path segment is the code. Take the one on the `county` line —
`NVC031`. The forecast zone sits directly beside it in the same response and
looks just as official, which is exactly how the wrong one gets copied into a
config file. Repeat for each town you want covered.

**Or list every county in a state and pick by name.**

```bash
curl -s -H "User-Agent: wx-alert/1.0 (you@yourdomain.org)" \
  "https://api.weather.gov/zones?type=county&area=NV" \
  | python3 -c 'import json,sys; [print(f["properties"]["id"], f["properties"]["name"]) for f in json.load(sys.stdin)["features"]]' \
  | sort
```

```
NVC001 Churchill
NVC003 Clark
NVC005 Douglas
NVC007 Elko
...
NVC019 Lyon
NVC029 Storey
NVC031 Washoe
NVC510 Carson City
```

Seventeen entries for Nevada, which is one more than it has counties: Carson
City is an independent city and gets its own code. Virginia has dozens of
these, so list rather than assume.

The three digits are the county's FIPS code, so `NVC031` is Nevada FIPS 031,
Washoe. If you already know a county's FIPS number you can construct the code
directly.

**Then confirm what the program actually queried.** Whatever you configure, the
startup log states the resolved county and the full query set. Check it once
after any change:

```
INFO Resolved monitored point to county zone=NVC031 latitude=39.5296 longitude=-119.8138
INFO Monitoring zones=NVC031,NVC029,NVC019 radius=50km point=39.5296,-119.8138
```

If any code in that list has a `Z` in the third position, storm-based warnings
from that zone will never arrive and nothing further will be logged about it.
A bad code is rejected at startup, but a *valid* forecast zone code is accepted
and simply returns less — which is why this is worth one look at the log rather
than trusting the config file.

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

`--meshcore-startup-max-age 0` disables the radio's startup staleness limit for
this run, so currently active alerts are rendered rather than suppressed as
old news. (`--startup-max-age` governs ntfy and has no effect on the radio.)

```bash
docker compose run --rm wx-alert \
  --once --meshcore --meshcore-dry-run --meshcore-startup-max-age 0
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

**Relevance.** `MIN_CLASS` and `MIN_SEVERITY` must both be satisfied. This gate
is the only one whose decision is final, because it is derived purely from the
alert's own content: the same product will always be judged the same way, so
there is nothing to reconsider later.

**Rate limiting.** `MIN_SECONDS_BETWEEN_SENDS` and `MAX_SENDS_PER_HOUR` bound
consumption regardless of relevance.

Minimum spacing is waited out within the cycle. A county query commonly returns
several active alerts at once, and refusing every one after the first would put
a single alert per poll interval on the air — at a five minute interval, half an
hour to clear six alerts, by which point a Flash Flood Warning has expired.
Spacing longer than two minutes is not waited for, since holding the cycle open
that long delays the next NWS query and everything behind it.

Hourly capacity is not waited for at all; the alert is simply offered again on
the next cycle for as long as NWS still lists it as active. Neither limit
records anything: an alert refused for want of airtime is **deferred, never
settled**, or a full budget would permanently discard a warning that a batch of
statements had crowded out.

Alerts are dispatched loudest first, by product class and then CAP severity, so
a budget that runs out mid-batch is spent on the most urgent products rather
than on whichever ones NWS happened to list first.

**Startup staleness.** The NWS API returns every *currently active* alert, not
only newly issued ones, so a restart is indistinguishable from a burst of new
alerts. On the first cycle, products older than `STARTUP_MAX_AGE_SECONDS` are
recorded as suppressed rather than announced. Unlike a rate-limit refusal this
one is deliberately final: the alert is old news, and re-offering it on the
second cycle would announce it four minutes later and achieve nothing.

Because it is final, the decision has three parts rather than one.

- An alert this transport has **already recorded an outcome for** is never
  treated as backlog. Either NWS reissued the product, or the previous attempt
  failed and this is the retry — and a radio that was unreachable throughout a
  warning must not have that warning written off the moment it recovers.
- On ntfy, active warnings bypass the limit unconditionally, because silently
  swallowing an ongoing warning is worse than a duplicate notification.
- On the radio they do not, because a duplicate push costs nothing while
  replaying hours-old warnings onto a shared channel after every restart costs
  everyone airtime. Instead the radio asks how much life a warning has left: a
  warning still in force for at least `STARTUP_MAX_AGE_SECONDS` is broadcast
  however old it is, and one about to expire is not. How old a warning is says
  nothing about whether it still matters; the time it has left says exactly
  that.

The practical case this covers: a Flash Flood Warning issued 50 minutes ago and
in force for another two hours, when the container restarts mid-event — which
`restart: unless-stopped` and `EXIT_AFTER_FAILED_CYCLES` make a routine event,
not an unusual one. It is old by age and it is the most important thing
happening.

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
levels, and only one of them is a risk to a repeat:

- **Mesh layer.** `MeshTables::hasSeen()` keeps a cyclic table of SHA-256
  hashes over the payload, and repeaters use it to stop flood packets looping.
  It cannot affect repeats: the hash covers the whole payload, and two copies
  differ in their timestamp, their marker and their cipher MAC.
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

## Region scoping

Optional, and off unless you set it. `[meshcore] SCOPE` restricts which
repeaters will relay these alerts.

### What a scope actually does

A MeshCore region is a name that hashes to a 16-byte transport key. Scoping a
message does **not** filter anything at this end. It makes the packet
forwardable only by repeaters configured with that region; every repeater
without it drops the packet rather than relaying it.

That inverts what the word suggests. A narrow scope does not deliver to a
smaller area — it delivers to whatever part of that area happens to be covered
by correctly configured repeaters.

### The failure mode to understand before switching this on

**Scope to a region no repeater in range carries, and the alert goes nowhere.**
Direct neighbours still hear the transmission, nothing forwards it, and no
error appears anywhere. The radio reports a successful send, the logs look
normal, and coverage has silently collapsed to line of sight.

So: pick a region broad enough to cover the whole area the alerts describe.
For a deployment covering northwestern Nevada, that means a regional name
rather than a single town. Confirm reception on a distant node before trusting
a scope in production.

### Setting it

```ini
[meshcore]
SCOPE = nnv
```

The leading `#` is optional; `nnv` and `#nnv` are the same region. Blank
means unscoped.

The key is a hash of the **exact** name, so `nnv` and `NNV` are different
regions that look nearly identical in a config file. Match whatever your
repeaters use, character for character. To make a mismatch diagnosable, the
resolved key is logged at startup and on every transmission:

```
MeshCore connected port=/dev/meshcore node='LNM-WXA' channel=1 scope=#nnv key=0e2b...
```

Compare that key against your repeater's region configuration. If the keys
differ, the names differ, however alike they look.

### Blank versus forced-unscoped

These are different, and the difference only shows on a radio with a default
scope set on the device itself:

| `SCOPE` | Behaviour |
|---|---|
| blank | The radio's scope state is not touched. A device default is respected. |
| `*` | Explicitly unscoped, overriding any device default. |
| a name | Scoped to that region. |

Blank is the default because it cannot change what an existing deployment
does, and because silently overriding a setting someone deliberately made on
the radio would be worse than inheriting it.

### When it fails

Region scoping needs MeshCore firmware **1.12.0 or newer**. If a scope is
configured and the radio rejects the command, the container refuses to start.
That is deliberate: the alternative is transmitting mesh-wide while the
operator believes the traffic is contained, and a warning in a log nobody
reads does not prevent that. With `SCOPE` blank the command is never issued,
so older firmware is unaffected.

The scope is device state rather than part of the message, so it is asserted
after connecting *and* before every transmission. A hard reset reboots the
radio and does not preserve it — without re-asserting, a radio that recovered
from a wedge mid-run would quietly resume transmitting outside its region.

To try a region without editing config:

```bash
docker compose run --rm wx-alert --meshcore-scope nnv --meshcore-test
```

That proves the radio accepts the region. It cannot prove anything will relay
it, which is the part that matters. For that, see below.

### Validating a region before you trust it

`--meshcore-test` transmits and stops. Nothing comes back, so a region that no
repeater carries passes it exactly like one that works. Checking coverage
needs something at the far end that answers.

Many meshes run a test channel with a bot that replies to `test` with the path
the message took and the scope it arrived under. If yours does, point the
probe at it:

```bash
# Find the test channel's index
docker compose run --rm wx-alert --meshcore-channels

# Not there? Add it. Lands in the first free slot and prints the index.
docker compose run --rm wx-alert --meshcore-add-channel '#test'

# Probe a candidate region on it
docker compose run --rm wx-alert \
  --meshcore-scope-probe nnv --meshcore-probe-channel 3
```

Only names starting with `#` can be added this way, and the restriction is
load-bearing. The firmware derives a hash-named channel's key from the name
itself, so every node that adds `#test` arrives at the same key with nothing
exchanged out of band. A channel without the `#` has a shared secret this
cannot guess, and inventing one would create a channel no one else can read —
which from here is indistinguishable from a channel where nobody is talking.

An occupied slot is refused rather than overwritten, since the displaced
channel's key cannot be recovered. The channel is read back after writing,
because trusting the acknowledgement would resurface later as a bot that never
answers.

The probe sends the **scoped** message first. If the bot answers, that settles
it and nothing else is sent. Only when the scoped leg draws no reply does an
unscoped control follow, to separate "nothing carries this region" from "the
bot answers nobody". A probe that reports *inconclusive* is telling you the
control failed too, so the region was never really tested.

That order is load-bearing, and getting it wrong once already produced a
confidently wrong answer. Both legs send the same trigger word, receiving
clients drop repeated identical content — the same behaviour alert repeats
carry `1/2` and `2/2` markers to work around — and whichever leg goes second
can be swallowed by it. With the control first, a bot that ignores the repeat
eats the scoped leg and a perfectly good region is reported broken. Sending
the scoped leg first puts that risk on the control, where losing it downgrades
the result to inconclusive instead of condemning the region. The probe also
waits before the control for the same reason.

The consequence worth remembering: a *positive* result is strong evidence, and
a negative one is worth repeating before you act on it.

Read the result in two parts. The verdict covers whether anything relayed the
scoped message; the bot's own reply, printed verbatim, tells you which region
it saw. Those answer different questions, and both need to be right: a bot
that replies but reports no scope means the radio is not applying one, whatever
the hop count says.

One case is deliberately *not* treated as success. If the scoped reply comes
back direct while the control was relayed, the bot simply heard the
transmission itself and no repeater is known to have forwarded it. That proves
the radio accepted the region and nothing more. Probe from somewhere that
needs a relay to reach the bot.

On the Northern Nevada mesh, `nnv` is confirmed working: it resolves to key
`4f1dac9ea4408c9a8e9759c9d80fbd67`, and a probe scoped to it was relayed to
the test bot three hops out, which reported `Region: nnv` back.

The probe restores the radio to its own default scope when it finishes,
including after a failure, so it cannot leave the device stuck in a region
nobody intended. It exits non-zero when the region does not check out, so it
can be scripted.

Neither flag runs alerts: both talk to the radio and exit. Both need the radio,
so stop the running container first.

---

## Persistent state

`/data/notified-alerts.json` records what has been delivered, per transport.
Mount it as a volume, as `docker-compose.yml` does. Without it, every restart
re-announces every active alert — noise over HTTPS, and a burst of
flood-routed traffic on LoRa.

`/data/resolved-zones.json` caches the county resolved from the monitored
point, so the `/points` lookup happens once rather than on every start. It is
discarded automatically if the coordinates change, and deleting it is harmless.

Recording is per transport because they fail independently: an ntfy success
paired with a radio failure needs a representation, or the next cycle either
duplicates the notification or permanently skips the broadcast.

An alert is considered handled only when its **content fingerprint** matches.
NWS reissues products under the original ID when details change, so comparing
IDs alone would silently swallow updates.

Only *terminal* outcomes are recorded: delivered, or skipped by a decision that
cannot change. An alert declined for a reason that clears on its own — no
airtime budget left, most often — is deferred and deliberately left absent from
the file, so the next cycle picks it up again. Recording those would retire an
alert over a condition that had already passed, which is how a warning could be
fetched correctly and then never transmitted at all.

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

Whether channel copies get deduplicated was open for two versions. It is now
answered: the losses are ordinary radio losses, and deduplication is not
involved. What remains open is the loss *rate*, which is why this stays here.

Four trials, all on `#rno-wx-alerts`:

| Trial | Sent | Arrived |
|---|---|---|
| Paired A/B test | 2 byte-identical, 2 marked | 1 of the identical, 2 of the marked |
| `--meshcore-test` self-test | 2 marked | 2 |
| Live Flash Flood Warning, 135/141 bytes | 2 marked | 1 (`1/2` arrived, `2/2` never did) |
| `--meshcore-test` self-test | 2 marked | 2 |

Marked copies are 7 of 8, and the third trial is the one that decides it: a
*marked* copy went missing. Marked copies differ in their content, so no
deduplication that looks at the message can explain the loss, which leaves the
plain radio loss the repeat exists to cover. That makes the identical copy lost
in the first trial unremarkable rather than suspicious — this link drops
roughly one copy in eight whether or not the copies are distinguishable.

The source agrees, and more firmly than the argument here used to.
`Packet::calculatePacketHash()` hashes the payload type and then the entire
payload, `sha.update(payload, payload_len)`, and `SimpleMeshTables::hasSeen()`
compares full hashes by `memcmp` against a 160-entry cyclic table. A group text
payload is a channel-hash byte, a two-byte cipher MAC, then ciphertext over a
four-byte timestamp, a flags byte and the text. Three fields therefore differ
between two marked copies: the timestamp, the marker, and the MAC that covers
them. The failure mode worth ruling out was a hash over only a *prefix* of the
payload, because the marker sits at the end of the text where a prefix hash
would not see it. The hash covers the whole payload.

The coarse-clock explanation is dead for the same reason it always was. If the
timestamp inside the hash were rounded to, say, the minute, copies seconds
apart would hash identically and the mesh would drop the repeat as a designed
behaviour. It is not rounded. `BaseChatMesh::sendGroupMessage()` copies the
full 32-bit seconds value into the payload with `memcpy(temp, &timestamp, 4)`,
commented *"mostly an extra blob to help make packet_hash unique"* — making the
hash differ is the whole point of the field. The value comes from the host,
which stamps `int(time.time())` per send, and the companion protocol carries it
as seconds. Nothing in that path quantises.

The open question is now the loss rate, and there is a candidate better than
chance. The lost copy carried a live warning at 135 of the 141 available bytes;
both clean self-tests carried short text. Longer packets occupy the channel
longer and have more to corrupt, so copy loss should scale with message length.
If that holds, real alerts — which run to the byte budget — lose copies more
often than a self-test will ever reveal, and two clean self-tests are weak
evidence that the repeat works when it matters. Testing that means sending
padded messages at several lengths and counting arrivals per length.

Two limits on the data above. The receiving device was not recorded per trial,
so the trials cannot be compared against each other for path quality. And the
fourth repeats the second's conditions rather than adding a new one.

Should a future loss still look like deduplication, the firmware counts it
instead of leaving it to inference: `SimpleMeshTables` tracks `_flood_dups` and
`_direct_dups`, exposed through the companion protocol as the flood and direct
route duplicate counts, alongside a stats reset. A copy dropped as a duplicate
increments them. A copy lost on the air does not.

The marker stays regardless: it costs four bytes, and it tells a reader that
they are looking at one warning rather than two.

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

# Try a region scope without editing config, and confirm the radio accepts it
docker compose run --rm wx-alert --meshcore-scope nnv --meshcore-test

# List the radio's channels, to find the one a probe bot listens on
docker compose run --rm wx-alert --meshcore-channels

# Check a region is actually relayed, using a bot that answers on channel 3
docker compose run --rm wx-alert \
  --meshcore-scope-probe nnv --meshcore-probe-channel 3

# Full alert text to stdout and to ntfy
docker compose run --rm wx-alert --ntfy --loop --verbose

# Preview radio output for one cycle without transmitting
docker compose run --rm wx-alert \
  --meshcore --meshcore-dry-run --once --meshcore-startup-max-age 0

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
