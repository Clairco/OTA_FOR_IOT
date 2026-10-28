# OTA for IoT devices

Pushes code updates to the Raspberry Pi devices (thermopile, PCS) over MQTT.

```
laptop                         MQTT broker                    device
server/ota_publisher_*.py ──► ota/updates/<target>/... ──► device/ota_agent.py
(pack + chunk the repo)                                      (verify, replace PROD_DIR, restart pm2)
```

## Components


| File                                 | Runs on                                 | What it does                                                                                                                   |
| ------------------------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `server/ota_publisher_thermopile.py` | Developer laptop                        | Packs the thermopile code folder into `repo_bundle-thermopile-<timestamp>.tar.gz` and publishes it to `ota/updates/thermopile` |
| `server/ota_publisher_pcs.py`        | Developer laptop                        | Same, for PCS (`ota/updates/PCSBLRCLX`)                                                                                        |
| `device/ota_agent.py`                | Each device, pm2 process `ota` (verify) | Receives the bundle, checks its SHA-256, replaces `PROD_DIR`, restarts the pm2 processes                                       |




## Device setup

1. Copy `device/.env.example` to `.env` next to `ota_agent.py` and fill it in:
  - `MQTT_BASE_TOPIC`: the topic this device listens on, e.g. `ota/updates/thermopile`
  - `STAGING_DIR`: temporary extract folder
  - `PROD_DIR`: the live code folder that gets replaced
2. The broker IP is **not** in `.env`. It's read from `static_ip_config.staticIp` in `/home/clairco/Desktop/OTA/iot_data.db`, which is set through the device app (`app.py`).
3. Start it: `pm2 start ota_agent.py --name ota --interpreter python3 && pm2 save`



## Pushing an update

1. Set `REPO_PATHS` in the publisher to the code folder you want to ship.
2. Run `python3 server/ota_publisher_thermopile.py` (or the PCS one).
3. Each device on that topic replies on `OTA/ack` with `{"mac_Id": ..., "status": "updated"}`.



## Read before pushing

- **The whole** `PROD_DIR` **is replaced**, including the device's `.env` and `logs/`. The bundle contains whatever is in your local folder (its `.env`, logs, uncommitted edits), so clean the folder first.
- **No git check:** the publisher always ships the current folder, whatever branch it's on.
- **Only** `scheduler`**,** `app` **and** `MQTT_Control_Consumer` **are restarted.** `websocketClient` and the agent itself keep running the old code.
- **The agent can't update itself:** changing `ota_agent.py` needs SSH or access to the device.
- **Devices that are offline during a push don't get the update.** Push again once they're back.
- **New Python dependencies aren't installed.** Install them on the device by hand.

