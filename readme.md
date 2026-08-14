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

`config.ini` is tracked in git as a template.  The only real 'secrets' are 
the ntfy.sh TOPIC values.  But it's hard to call this a security consideration.  

## Other Items of Interest

# NWS Zones 
  XXCNNN - Makes it so you don't just get point-specific statements, alerts, and warnings, but also polygon-based alerts.  This is 
  handled by the ZONES directive in config.local.ini

# Meshcore Regions/Scopes
  Regions are configured on repeaters, messages are scoped to regions.  You may set SCOPE in config.local.ini, e.g. 'nnv' to limit the
  propagation of a locality-specific alert (Elko doesn't care if we are flooding, Reno doesn't care if Elko is under 18' of snow, etc.)

# Alert Radius
  ALERT_RADIUS_KM prevents alerting on a polygon-based warning if it is > ALERT_RADIUS_KM from the point defined by:
    DEFAULT_LATITUDE && DEFAULT_LONGITUDE

Minimum Product Class
  Initially meant to cover warnings and watches, it turns out everything down to 'statement' can have valuable and timely information, 
  allowing you to prepare ahead of time (minutes to days).  I recommend you start out with MIN_CLASS=statement and go from there.

# Severity
  The NWS uses CAP (Common Alerting Protocol) to set a severity on alerts.  This is used to indicate the level of threat to life and property.
  The problem here is that unless you live in tornado alley, setting this to severe could cause alerts to not forward over the mesh.  Setting 
  MIN_SEVERITY=unknown initially is a good idea - tune from there for your mesh or your needs.

# Repeat Sends
  This might torque some mesh users, but REPEAT_SENDS can be cranked up.  The idea is that mesh is imperfect (or less reliable than a standard TCP/IP connection, since we don't get solid ACKs for most Meshcore messages).  So 
  we send the alert twice (1/2 and 2/2) by default.  If you favor the reliability of your local mesh, drop it to 1.  
