# NWS alert to ntfy container

## 1. Configure

Edit `config.ini`. At minimum, set the latitude, longitude, and ntfy topic:

```ini
[weather]
DEFAULT_LATITUDE = 45.80694
DEFAULT_LONGITUDE = -108.5422

[ntfy]
TOPIC = your-long-unguessable-topic

[delivery]
# Delay between separate notifications when multiple alerts are found.
DELAY_SECONDS = 1

# Check interval - how often the script runs to check for new updates.
# Range: 5-3600 seconds. Default: 3600.
CHECK_INTERVAL = 3600
```

`CHECK_INTERVAL` controls the long-running container's polling interval. It is
not the delay between multiple notifications; that remains `DELAY_SECONDS`.

## 2. Build

```bash
docker build -t wx-alert:latest .
```

## 3. Test ntfy delivery

This sends one test notification, does not query NWS, and exits:

```bash
docker run --rm --name wx-alert-test wx-alert:latest --ntfy-test
```

## 4. Run continuously

The image defaults to `--ntfy --loop`. It performs its first NWS check
immediately, then repeats at `CHECK_INTERVAL`:

```bash
docker run -d \
  --name wx-alert \
  --restart unless-stopped \
  wx-alert:latest
```

Follow logs:

```bash
docker logs -f wx-alert
```

Stop it cleanly:

```bash
docker stop wx-alert
```

During one container run, an alert ID is notified only once. If ntfy delivery
fails, that alert remains eligible for retry on the next check. This duplicate
suppression is currently in memory and resets when the container restarts.

## One-time check

Override the Docker default command with `--ntfy --once`:

```bash
docker run --rm wx-alert:latest --ntfy --once
```

## Override the check interval from the command line

This does not change `config.ini`; it overrides it for this container run:

```bash
docker run --rm wx-alert:latest \
  --ntfy --loop --check-interval 300
```

## Verbose notification and output

Because an explicit Docker command replaces the Dockerfile `CMD`, include
`--ntfy --loop` when adding normal polling options:

```bash
docker run --rm wx-alert:latest --ntfy --loop --verbose
```

## Runtime config override

The default config is baked into the image. To supply a different config
without rebuilding, mount it read-only:

```bash
docker run -d \
  --name wx-alert \
  --restart unless-stopped \
  --mount type=bind,src="$PWD/config.ini",dst=/app/config.ini,readonly \
  wx-alert:latest
```

This is strongly recommended when `TOKEN` contains a secret.

## Logs

Operational logs are timestamped in UTC. Typical output includes:

```text
2026-07-14T20:00:00Z INFO Starting wx-alert mode=polling config=/app/config.ini
2026-07-14T20:00:00Z INFO Polling enabled check_interval=3600 seconds
2026-07-14T20:00:00Z INFO Beginning check cycle=1
2026-07-14T20:00:01Z INFO NWS returned 2 active alert(s); 2 new alert(s)
2026-07-14T20:00:03Z INFO Next NWS check in 3600 second(s)
```

## Exit behavior

- `--ntfy-test` sends one test notification and exits.
- `--once` performs one check and exits.
- `--loop` continues through temporary NWS or ntfy failures and exits cleanly
  on `docker stop`, SIGTERM, or Ctrl-C.
