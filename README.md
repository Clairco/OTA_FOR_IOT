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
| `server/ota_publisher_thermopile.py` | Developer laptop                        | Packs the thermopile code folder into `repo_bundle-thermopile-<timestamp>.tar.gz` and publishes it to `ota/updates/thermopile`. Reads `targets.config.json` and MongoDB to decide which devices in the group this push targets; prompts for confirmation before publishing. |
| `server/ota_publisher_pcs.py`        | Developer laptop                        | Same, for PCS (`ota/updates/PCSBLRCLX`). Publishes to every device in the group (no target filter).                                                                                        |
| `server/ota_publisher_agent.py`      | Developer laptop                        | **Agent self-update pipeline.** Ships a new `ota_agent.py` and/or `agent.env` to the fleet over the separate `OTA_AGENT` MQTT topic. Password-gated, requires a typed risk-acknowledgement phrase, and writes an append-only audit log. See [Agent self-update](#agent-self-update-updating-ota_agentpy). |
| `device/ota_agent.py`                | Each device, pm2 process `ota` (verify) | Receives the bundle, checks the manifest's target list, verifies SHA-256, replaces `PROD_DIR`, restarts the pm2 processes. Also listens on `OTA_AGENT` for agent self-updates and runs the candidate agent with `--selfcheck` before committing the swap. |




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

1. Set `REPO_PATHS` in `server/publisher.env` to the code folder you want to ship.
2. Pick which devices in the group this push goes to — see [Selective delivery](#selective-delivery-adding-devices-and-picking-a-mode) below.
3. Run `python3 server/ota_publisher_thermopile.py` (or the PCS one). The thermopile publisher queries MongoDB, prints the plan (group size, mode, device list, effective target count), and asks for confirmation before building the archive or opening MQTT. Type `y` or `yes` to proceed.
4. Each targeted device replies on `OTA/ack` with `{"mac": ..., "status": "updated"}`. Non-targeted devices in the group see the manifest, log that they aren't targeted, and ignore the chunks.
5. The resolved target list (what the publisher actually sent) is written to `server/targets.resolved.json` as an audit record.

### Selective delivery: adding devices and picking a mode

The thermopile publisher reads `server/targets.config.json` to decide which devices inside the MQTT group actually install this push. The file is git-ignored; copy `server/targets.config.example.json` the first time.

**File format**

```json
{
  "mode": "include",
  "devices": [
    "thermopile-lab-01",
    "thermopile-lab-02"
  ]
}
```

**`mode`** — one of:

| Mode      | Who installs the push                                                                 | `devices` list                |
| --------- | ------------------------------------------------------------------------------------- | ----------------------------- |
| `all`     | Every device in the group currently known to MongoDB.                                 | Ignored (can be omitted).     |
| `include` | Only the devices named in `devices`.                                                  | Required, non-empty.          |
| `exclude` | Every device in the group *except* the ones named in `devices` (safe way to skip a known-broken unit). | Required, non-empty.          |

**`devices`** — a list of device **names**, not MAC addresses. Each name must match the `name` field (`MONGO_FIELD_NAME_DEFAULT = "sensorName"`) on a document in the Mongo collection pointed at by `MONGO_COLLECTION` in `publisher.env`. The publisher looks up each name, pulls its `macAddress`, and embeds the resulting MAC list (or its complement, whichever is shorter) in the manifest. Unknown names abort the push before anything is sent.

**Adding a new device to a push**

1. Make sure the device is registered in MongoDB with a `sensorName` and `macAddress` (this happens through the device app, not this repo).
2. Open `server/targets.config.json`.
3. Set `mode` to `include` (or `exclude`, depending on intent) and add the device's `sensorName` to the `devices` list. For an all-devices push, set `mode` to `"all"`.
4. Run the publisher. The plan it prints shows:
   - the group (MQTT topic suffix),
   - how many devices MongoDB knows about,
   - the chosen mode,
   - the full list of devices to include or exclude,
   - the effective target count.
   Review it, then type `y` to proceed.

**On the device side**, [ota_agent.py](device/ota_agent.py) checks the manifest's `targets` block in `on_message` ([ota_agent.py:472-476](device/ota_agent.py#L472-L476)): if this MAC is not in the effective set, the agent logs `Session … does not target this device, skipping` and drops the chunks without touching `PROD_DIR`. Unknown modes fail closed, so a malformed manifest never installs anywhere.

> **PCS publisher note:** `ota_publisher_pcs.py` does not currently read `targets.config.json` — every device in the PCS group gets every push.



## Agent self-update: updating `ota_agent.py`

The normal publisher cannot update the OTA agent itself — the running agent is what receives and applies the bundle, so swapping it out mid-session would break the current session. [`server/ota_publisher_agent.py`](server/ota_publisher_agent.py) is a separate pipeline that handles this, with extra safeguards because a broken release here requires **physical access to every affected device** to recover.

```
laptop                                    MQTT broker              device
server/ota_publisher_agent.py ──► OTA_AGENT/<session>/... ──► device/ota_agent.py
(password + risk prompt, pack            (separate from the normal           (selfcheck candidate, swap
 ota_agent.py / agent.env)                ota/updates topic tree)             ota_agent.py + agent.env, restart `ota`)
```

### What it can ship

A bundle (`agent_bundle-<timestamp>.tar.gz`) with any combination of:

| Bundle entry           | Purpose                                                                |
| ---------------------- | ---------------------------------------------------------------------- |
| `ota_agent.py`         | Replacement for the running agent script on each device.               |
| `agent.env`            | Full replacement `agent.env` (overwrites what's there).                |
| `agent_env_patch.json` | Diff for `agent.env`: `set`, `set_if_missing`, `remove`, `targets`. Mutually exclusive with `agent.env`. |

At least one of the three must be present. A bundle containing both `agent.env` and `agent_env_patch.json` is rejected on the device.

### First-time setup

1. Decide the list of devices this push will go to — copy `server/agent_targets.config.example.json` to `server/agent_targets.config.json` (git-ignored) and set `mode` + `devices` (same format as [Selective delivery](#selective-delivery-adding-devices-and-picking-a-mode)). This is intentionally a **separate file** from `targets.config.json` so you don't accidentally reuse the last normal-OTA target set.
2. On the very first run, the publisher sees no `server/agent_publisher.config.json` and prompts you to set the agent-OTA password (min 8 chars). It stores a PBKDF2 hash in that file (git-ignored, `0600` on POSIX).
   - Rotating the password = delete `server/agent_publisher.config.json` and run the publisher once to re-prompt.

### Publishing

From `server/`, with `publisher.env` already configured (same MQTT + Mongo creds the normal publisher uses):

```bash
# Ship only a new agent script
python3 ota_publisher_agent.py --script /path/to/new/ota_agent.py

# Ship only an env change (patch diff)
python3 ota_publisher_agent.py --agent-env-patch /path/to/agent_env_patch.json

# Ship both
python3 ota_publisher_agent.py --script /path/to/new/ota_agent.py --agent-env-patch /path/to/agent_env_patch.json
```

Available flags:

| Flag                  | Required                  | Meaning                                                  |
| --------------------- | ------------------------- | -------------------------------------------------------- |
| `--script`            | one of these three        | Path to the new `ota_agent.py`.                          |
| `--agent-env`         | one of these three        | Path to a full replacement `agent.env`.                  |
| `--agent-env-patch`   | one of these three        | Path to an `agent_env_patch.json` (mutually exclusive with `--agent-env`). |
| `--chunk-size`        | optional (default 256 KB) | MQTT chunk size in bytes.                                |
| `--qos`               | optional (default 1)      | MQTT QoS: 0, 1, or 2.                                    |

The publisher then runs this sequence, aborting on the first failure:

1. **Pre-flight validation** — resolves the input paths; compiles `--script` with `py_compile` to catch syntax errors; validates `--agent-env-patch` JSON structure (`set` / `set_if_missing` / `remove` / `targets` sections only, keys match `^[A-Za-z_][A-Za-z0-9_]*$`, values are strings).
2. **Password gate** — prompts for the agent-OTA password and compares (`hmac.compare_digest`) against the stored PBKDF2 hash. **No retries**: one wrong entry aborts.
3. **Target resolution** — reads `agent_targets.config.json`, looks up each `sensorName` in MongoDB, prints the effective MAC list.
4. **Risk acknowledgement** — prints a warning block and asks you to type the exact phrase `I UNDERSTAND`. Anything else aborts.
5. **Publish** — builds the tarball, publishes manifest + chunks + complete on `OTA_AGENT/<session_id>/...`. Audit record written to `server/agent_targets.resolved.json`.

Every step (invocation, password accepted, risk acknowledged, published, aborted, failed) is appended as a JSON line to [`server/agent_publisher.audit.log`](server/agent_publisher.audit.log).

### Device-side handling

[`ota_agent.py`](device/ota_agent.py) subscribes to `OTA_AGENT/+/...` in parallel to the normal OTA topic. For each agent-OTA bundle the agent:

1. Verifies SHA-256 and extracts into `agent_staging/` (not `STAGING_DIR`).
2. Rejects the bundle if it contains **both** `agent.env` and `agent_env_patch.json`.
3. Spawns the **candidate** `ota_agent.py --selfcheck` as a subprocess. The selfcheck validates imports, both env files, and MQTT broker reachability using a different client-id suffix so it doesn't kick the running agent off the broker. Non-zero exit codes map to specific failure reasons in the ack (`agent_env_missing`, `agent_env_invalid`, `app_env_missing`, `mqtt_connect`, …).
4. On selfcheck success: swaps `ota_agent.py` and `agent.env` atomically (with `.bak` copies preserved), acks on `OTA/ack` with `{"module": "ota_agent", "status": "updated"}`, and restarts itself via `pm2 restart ota`.
5. On any failure: restores from `.bak`, acks with `{"module": "ota_agent", "status": "failed", "stage": ..., "reason": ...}`, and keeps running the old agent.

An incoming agent-OTA manifest is rejected (with an `ACK_STAGE_CONCURRENCY` ack) if **any** other session is already in flight on the device — the self-update defers and the publisher is expected to retry. The guard is one-way: a normal-OTA manifest that arrives mid-agent-OTA is not rejected, because agent OTA is the high-risk pipeline that should step aside.



## Read before pushing

- **The whole** `PROD_DIR` **is replaced**, except `app.env`, `app.env.bak`, `logs/` and `iot_data.db`, which stay on the device. The bundle contains whatever is in your local folder (uncommitted edits), so clean the folder first.
- **`app.env` comes from `server/app.env`, not from the code folder.** Paste it there before publishing (it is git-ignored). A device with no `app.env` gets a copy of it; a device that has one keeps its own and only takes an `env_patch.json` from the bundle.
- **No git check:** the publisher always ships the current folder, whatever branch it's on.
- **Only** `scheduler`**,** `app` **and** `MQTT_Control_Consumer` **are restarted,** then the agent itself (`ota`) a few seconds later. `websocketClient` keeps running the old code.
- **Updating `ota_agent.py` or `agent.env` is a separate pipeline** — the normal publisher cannot ship them. See [Agent self-update](#agent-self-update-updating-ota_agentpy).
- **Devices that are offline during a push don't get the update.** Push again once they're back.
- **New Python dependencies aren't installed.** Install them on the device by hand.

## Do not update THERMAL_SENSOR through OTA

`THERMAL_SENSOR` in a device's `prod/app.env` selects its sensor and is set once at device setup. Do not put it in the `app.env` that the publisher ships (`server/app.env`) and do not set, change or remove it with `env_patch.json`. Devices without the key use the Heimann sensor.
