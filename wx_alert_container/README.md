# NWS alert to ntfy container

## 1. Configure

Edit `config.ini`. At minimum, set:

```ini
[ntfy]
TOPIC = your-long-unguessable-topic
```

Also set the latitude and longitude under `[weather]`.

## 2. Build

```bash
docker build -t wx-alert:latest .
```

## 3. Test ntfy delivery

This sends one test notification, does not query NWS, and exits:

```bash
docker run --rm --name wx-alert-test wx-alert:latest --ntfy-test
```

## 4. Perform one alert check

The image defaults to `--ntfy`, so this queries NWS once, sends each active
alert as a separate ntfy notification, and exits:

```bash
docker run --name wx-alert wx-alert:latest
```

Review logs:

```bash
docker logs wx-alert
```

Remove the stopped one-shot container when finished:

```bash
docker rm wx-alert
```

For a disposable run:

```bash
docker run --rm wx-alert:latest
```

## Verbose notification and output

Because an explicit Docker command replaces the Dockerfile `CMD`, include
`--ntfy` when adding other normal-check options:

```bash
docker run --rm wx-alert:latest --ntfy --verbose
```

## Runtime config override

The default config is baked into the image. To supply a different config
without rebuilding, mount it read-only:

```bash
docker run --rm \
  --mount type=bind,src="$PWD/config.ini",dst=/app/config.ini,readonly \
  wx-alert:latest
```

This is strongly recommended when `TOKEN` contains a secret.

## Exit codes

- `0`: successful check/test; no alerts is also success
- `1`: NWS request or response failure
- `2`: ntfy delivery/test failure or command/configuration error

## Scheduling

The container performs one check and exits. Schedule it with host cron,
a systemd timer, Kubernetes CronJob, or another scheduler. Persistent
alert-ID tracking is not yet implemented, so repeated scheduled runs will
re-send alerts that remain active.
