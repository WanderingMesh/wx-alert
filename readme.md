# wx-alert

Monitors National Weather Service active alerts for a single geographic point
and republishes each new or updated alert to one or more delivery transports.

Supported transports:

- **ntfy** — push notification to an ntfy topic over HTTPS.
- **MeshCore** — broadcast to a MeshCore channel via a USB-attached companion
  radio. Intended for off-grid delivery over LoRa.

The program runs as a Docker container. See
[`wx_alert_container/README.md`](wx_alert_container/README.md) for
configuration, build, and deployment instructions.

## Repository layout

```
wx_alert_container/     Docker build context and application source
  wx_alert/             Application package
  config.ini            Configuration template (no secrets)
  Dockerfile            Multi-stage build, runs as an unprivileged user
  docker-compose.yml    Deployment, including serial device passthrough
  tests/                Test suite
```

## Configuration secrets

`config.ini` is tracked in git as a template and must not contain real
credentials. The ntfy `TOPIC` and `TOKEN` both act as bearer secrets: anyone
holding a topic name can read and publish to it. Supply real values by
bind-mounting a config file at runtime, as described in the container README.
