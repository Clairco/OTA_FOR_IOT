# OTA for IoT devices

Pushes code updates to the CM4/Raspberry Pi devices (thermopile, PCS) over MQTT.

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

1. Copy `device/agent.env.example` to `agent.env` next to `ota_agent.py` and fill it in:
  - `MQTT_BASE_TOPIC`: the topic this device listens on, e.g. `ota/updates/thermopile`
  - `STAGING_DIR`: temporary extract folder
  - `PROD_DIR`: the live code folder that gets replaced
2. Create `prod/app.env` (next to the agent, in `prod/`) with `MQTT_PORT`, `MQTT_USERNAME` and `MQTT_PASSWORD`, plus the app's own settings. The agent reads its MQTT credentials from here and exits with an error if the file or a key is missing.
3. The broker IP is **not** in either env file. It's read from `static_ip_config.staticIp` in `/home/clairco/Desktop/OTA/iot_data.db`, which is set through the device app (`app.py`).
4. Start it as a pm2 process named `ota`. The name must be exactly `ota`: after an update the agent restarts itself with `pm2 restart ota`.
  ```
  pm2 start /home/clairco/Desktop/OTA/ota_agent.py --name ota --interpreter python3
  pm2 save
  ```
  The agent finds `agent.env` and `prod/app.env` relative to its own file, so it can be started from any directory. Check it with `pm2 logs ota`.



## Pushing an update

1. Set `REPO_PATHS` in the publisher to the code folder you want to ship.
2. Run `python3 server/ota_publisher_thermopile.py` (or the PCS one).
3. Each device on that topic replies on `OTA/ack` with `{"mac_Id": ..., "status": "updated"}`.



## Read before pushing

- **The whole** `PROD_DIR` **is replaced**, except `app.env`, `app.env.bak`, `logs/` and `iot_data.db`, which stay on the device. The bundle contains whatever is in your local folder (uncommitted edits), so clean the folder first.
- **`app.env` comes from `server/app.env`, not from the code folder.** Paste it there before publishing (it is git-ignored). A device with no `app.env` gets a copy of it; a device that has one keeps its own and only takes an `env_patch.json` from the bundle.
- **No git check:** the publisher always ships the current folder, whatever branch it's on.
- **Only** `scheduler`**,** `app` **and** `MQTT_Control_Consumer` **are restarted,** then the agent itself (`ota`) a few seconds later. `websocketClient` keeps running the old code.
- **The agent can't update itself:** changing `ota_agent.py` needs SSH or access to the device.
- **Devices that are offline during a push don't get the update.** Push again once they're back.
- **New Python dependencies aren't installed.** Install them on the device by hand.

## Do not update THERMAL_SENSOR through OTA

`THERMAL_SENSOR` in a device's `prod/app.env` selects its sensor and is set once at device setup. Do not put it in the `app.env` that the publisher ships (`server/app.env`) and do not set, change or remove it with `env_patch.json`. Devices without the key use the Heimann sensor.
