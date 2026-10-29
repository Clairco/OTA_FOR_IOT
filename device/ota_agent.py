import os
import shutil
import tarfile
import json
import hashlib
import platform
import re
import subprocess
import sys
import threading
import traceback
import sqlite3
from pathlib import Path
import paho.mqtt.client as paho
from dotenv import dotenv_values

# ==============================
# Configuration
# ==============================

# Paths are relative to this file so the agent works from any working directory (pm2, systemd).
AGENT_DIR = Path(__file__).resolve().parent
AGENT_ENV_PATH = AGENT_DIR / "agent.env"          # agent-only settings: topic and directories
APP_ENV_PATH = AGENT_DIR / "prod" / "app.env"     # app settings; the MQTT credentials are shared from here

ENV_KEY_MQTT_PORT = "MQTT_PORT"
ENV_KEY_MQTT_USERNAME = "MQTT_USERNAME"
ENV_KEY_MQTT_PASSWORD = "MQTT_PASSWORD"
ENV_KEY_MQTT_BASE_TOPIC = "MQTT_BASE_TOPIC"
ENV_KEY_STAGING_DIR = "STAGING_DIR"
ENV_KEY_PROD_DIR = "PROD_DIR"
APP_ENV_REQUIRED_KEYS = (ENV_KEY_MQTT_PORT, ENV_KEY_MQTT_USERNAME, ENV_KEY_MQTT_PASSWORD)
AGENT_ENV_REQUIRED_KEYS = (ENV_KEY_MQTT_BASE_TOPIC, ENV_KEY_STAGING_DIR, ENV_KEY_PROD_DIR)

EXIT_CODE_CONFIG_ERROR = 1


def exit_with_config_error(message: str):
    print(f"[error] {message}", file=sys.stderr)
    sys.exit(EXIT_CODE_CONFIG_ERROR)


def load_required_env(env_path: Path, required_keys) -> dict:
    """Read a .env file without touching os.environ; exit with an error when the file or a required key is missing."""
    if not env_path.is_file():
        exit_with_config_error(f"Env file missing: {env_path}")
    values = dotenv_values(env_path)
    missing_keys = [key for key in required_keys if not values.get(key)]
    if missing_keys:
        exit_with_config_error(f"Missing keys in {env_path}: {', '.join(missing_keys)}")
    return values


def get_broker_from_db():
    try:
        conn = sqlite3.connect("/home/clairco/Desktop/OTA/iot_data.db")
        cursor = conn.cursor()
        cursor.execute("SELECT staticIp FROM static_ip_config LIMIT 1;")
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
        else:
            raise Exception("No MQTT_BROKER found in static_ip_config table")
    except Exception as e:
        print(f"[error] Failed to fetch MQTT_BROKER from DB: {e}")
        quit()


APP_ENV = load_required_env(APP_ENV_PATH, APP_ENV_REQUIRED_KEYS)
AGENT_ENV = load_required_env(AGENT_ENV_PATH, AGENT_ENV_REQUIRED_KEYS)

BROKER = get_broker_from_db()
PORT = APP_ENV[ENV_KEY_MQTT_PORT]
USER = APP_ENV[ENV_KEY_MQTT_USERNAME]
PASSWORD = APP_ENV[ENV_KEY_MQTT_PASSWORD]
TOPIC = AGENT_ENV[ENV_KEY_MQTT_BASE_TOPIC]

STAGING_DIR = Path(AGENT_ENV[ENV_KEY_STAGING_DIR])          # temp extract location
LIVE_CODE_DIR = Path(AGENT_ENV[ENV_KEY_PROD_DIR])           # your running code location

# Device-owned entries at the top level of PROD_DIR. An update never deletes
# them and never takes them from the bundle, so each device keeps its own
# configuration and data.
PRESERVED_PATHS = frozenset({
    "app.env",      # device config: THERMAL_SENSOR, MQTT credentials, LOG_DIR, ...
    "app.env.bak",  # previous app.env, written by an env patch; needed to restore it
    "logs",         # device logs collected for diagnosis
    "iot_data.db",  # local zone/config database, when it lives in PROD_DIR
})

# A device without an app.env gets a copy of the bundle's one. A device that has one keeps it and only takes an
# env_patch.json. The bundle's app.env.example lists every key the new code
# reads; keys missing from the device app.env are reported in the ack.
DEVICE_ENV_FILE = "app.env"
EXAMPLE_ENV_FILE = "app.env.example"
ENV_COMMENT_PREFIX = "#"
ENV_EXPORT_PREFIX = "export "
ENV_KEY_SEPARATOR = "="
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Values containing any of these are written double-quoted.
ENV_CHARS_NEEDING_QUOTES = frozenset(" \t#\"'\\")

# Optional env_patch.json at the bundle root: edits an existing device app.env,
# since the bundle's app.env never overwrites it. Do not update THERMAL_SENSOR
# through OTA: each device's sensor is set at device setup. Format:
#   {
#     "set_if_missing": {"LOG_DIR": "logs"},     # add new keys, never overwrite
#     "set":            {"HEARTBEAT_INTERVAL": "300"},  # add or overwrite
#     "remove":         ["BROKER_ADDRESS"],      # delete keys
#     "targets":        {"mac": ["D8:3A:DD:00:00:01"]}  # optional: only these devices
#   }
# Values must be strings. Top-level keys starting with "_" (e.g. "_help") are
# notes for people and are ignored. The patch is an instruction, not code: it
# is never copied into PROD_DIR.
ENV_PATCH_FILE = "env_patch.json"
ENV_PATCH_NOTE_PREFIX = "_"
ENV_PATCH_SET_IF_MISSING = "set_if_missing"
ENV_PATCH_SET = "set"
ENV_PATCH_REMOVE = "remove"
ENV_PATCH_TARGETS = "targets"
ENV_PATCH_TARGET_MACS = "mac"
ENV_PATCH_SECTIONS = frozenset({ENV_PATCH_SET_IF_MISSING, ENV_PATCH_SET, ENV_PATCH_REMOVE, ENV_PATCH_TARGETS})
ENV_PATCH_STATUS_NONE = "none"
ENV_PATCH_STATUS_APPLIED = "applied"
ENV_PATCH_STATUS_NOT_TARGETED = "not_targeted"
ENV_PATCH_STATUS_SKIPPED_ENV_CREATED = "skipped_env_created"
ENV_BACKUP_SUFFIX = ".bak"
ENV_TMP_SUFFIX = ".tmp"
ENV_FILE_MODE = 0o600

# Bundle files that are read by the agent but never deployed.
NOT_DEPLOYED_PATHS = frozenset({ENV_PATCH_FILE})

ACK_TOPIC = "OTA/ack"
ACK_QOS = 2
ACK_STATUS_UPDATED = "updated"
ACK_STATUS_FAILED = "failed"

PM2_SERVICES_TO_RESTART = ("scheduler", "app", "MQTT_Control_Consumer")
PM2_AGENT_SERVICE = "ota"
# Restarting the agent kills this process, so it waits for the ack to go out first.
AGENT_RESTART_DELAY_IN_MS = 3000
MS_PER_SECOND = 1000

sessions = {}

# =============================
# get mac address
def get_ethernet_mac():
    """Get the Ethernet MAC address of the Raspberry Pi"""
    try:
        # For Raspberry Pi/Linux systems
        if platform.system() == "Linux":
            # Try to get eth0 MAC address using subprocess
            result = subprocess.run(['cat', '/sys/class/net/eth0/address'],
                                  capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip().upper()

        # Fallback method using uuid (gets the first available MAC)
        mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff)
                       for elements in range(0,2*6,2)][::-1])
        return mac.upper()

    except Exception as e:
        return f"Error: {str(e)}"
# ============================

MAC_ADDRESS = get_ethernet_mac()

# ==============================
# Safe extraction
# ==============================
def safe_extract(tar: tarfile.TarFile, path: Path):
    for member in tar.getmembers():
        member_path = (path / member.name).resolve()
        if not str(member_path).startswith(str(path.resolve())):
            raise Exception(f"Unsafe path in tar archive: {member.name}")
    tar.extractall(path)

# ==============================
# Replace live code
# ==============================
def replace_live_code(staging: Path, live: Path):
    live.mkdir(parents=True, exist_ok=True)
    for item in live.iterdir():
        if item.name in PRESERVED_PATHS:
            continue
        if item.is_file() or item.is_symlink():
            item.unlink()
        else:
            shutil.rmtree(item)
    for item in staging.iterdir():
        if item.name in PRESERVED_PATHS:      # never let the bundle overwrite them
            continue
        if item.name in NOT_DEPLOYED_PATHS:   # agent instructions, not code
            continue
        shutil.move(str(item), str(live / item.name))

# ==============================
# Device app.env checks
# ==============================
def parse_env_key(line: str):
    """Return the key defined on a .env line, or None for blank/comment/invalid lines."""
    line = line.strip()
    if not line or line.startswith(ENV_COMMENT_PREFIX) or ENV_KEY_SEPARATOR not in line:
        return None
    if line.startswith(ENV_EXPORT_PREFIX):
        line = line[len(ENV_EXPORT_PREFIX):]
    return line.split(ENV_KEY_SEPARATOR, 1)[0].strip()


def read_env_keys(env_path: Path) -> set:
    """Return the keys defined in a .env-style file (comments and blank lines ignored)."""
    keys = set()
    for line in env_path.read_text().splitlines():
        key = parse_env_key(line)
        if key:
            keys.add(key)
    return keys


def require_app_env_source(staging: Path, live: Path):
    """
    Raise RuntimeError when neither the device nor the bundle has an app.env.

    Without either, the new code would start with no app.env (wrong sensor,
    no credentials), so the update is not applied.
    """
    if not (live / DEVICE_ENV_FILE).is_file() and not (staging / DEVICE_ENV_FILE).is_file():
        raise RuntimeError(f"{DEVICE_ENV_FILE} missing on the device ({live}) and in the bundle, update not applied")


def write_env_lines_atomically(env_path: Path, lines: list):
    tmp_path = env_path.with_name(env_path.name + ENV_TMP_SUFFIX)
    tmp_path.write_text("\n".join(lines) + "\n")
    os.chmod(tmp_path, ENV_FILE_MODE)
    os.replace(tmp_path, env_path)


def create_app_env_from_bundle(bundle_env: Path, device_env: Path):
    write_env_lines_atomically(device_env, bundle_env.read_text().splitlines())


def find_missing_env_keys(staging: Path, device_env: Path) -> list:
    """Keys in the bundle's app.env.example that the device app.env does not define (empty when there is no example)."""
    example_env = staging / EXAMPLE_ENV_FILE
    if not example_env.is_file():
        return []
    return sorted(read_env_keys(example_env) - read_env_keys(device_env))

# ==============================
# Device app.env patch (env_patch.json)
# ==============================
def _validate_env_mapping(patch: dict, section: str):
    values = patch.get(section, {})
    if not isinstance(values, dict):
        raise ValueError(f"{ENV_PATCH_FILE}: '{section}' must be an object of KEY: \"value\"")
    for key, value in values.items():
        if not ENV_KEY_PATTERN.match(str(key)):
            raise ValueError(f"{ENV_PATCH_FILE}: invalid key '{key}' in '{section}'")
        if not isinstance(value, str):
            raise ValueError(f"{ENV_PATCH_FILE}: value of '{key}' in '{section}' must be a string")
        if "\n" in value or "\r" in value:
            raise ValueError(f"{ENV_PATCH_FILE}: value of '{key}' in '{section}' contains a line break")


def load_env_patch(staging: Path):
    """
    Read and validate env_patch.json from the bundle. Returns None when the
    bundle has none; raises ValueError when it is invalid, so a bad patch
    stops the update before anything on the device changes.
    """
    patch_path = staging / ENV_PATCH_FILE
    if not patch_path.is_file():
        return None
    try:
        patch = json.loads(patch_path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{ENV_PATCH_FILE} is not valid JSON: {e}") from e
    if not isinstance(patch, dict):
        raise ValueError(f"{ENV_PATCH_FILE} must be a JSON object")
    # "_help" and other "_" keys are notes for people, not instructions
    patch = {key: value for key, value in patch.items() if not str(key).startswith(ENV_PATCH_NOTE_PREFIX)}
    unknown = set(patch) - ENV_PATCH_SECTIONS
    if unknown:
        raise ValueError(f"{ENV_PATCH_FILE}: unknown sections {sorted(unknown)}")

    _validate_env_mapping(patch, ENV_PATCH_SET_IF_MISSING)
    _validate_env_mapping(patch, ENV_PATCH_SET)

    removed = patch.get(ENV_PATCH_REMOVE, [])
    if not isinstance(removed, list) or not all(isinstance(k, str) and ENV_KEY_PATTERN.match(k) for k in removed):
        raise ValueError(f"{ENV_PATCH_FILE}: '{ENV_PATCH_REMOVE}' must be a list of key names")
    written = set(patch.get(ENV_PATCH_SET, {})) | set(patch.get(ENV_PATCH_SET_IF_MISSING, {}))
    conflicts = written & set(removed)
    if conflicts:
        raise ValueError(f"{ENV_PATCH_FILE}: keys both set and removed: {sorted(conflicts)}")

    targets = patch.get(ENV_PATCH_TARGETS, {})
    if not isinstance(targets, dict):
        raise ValueError(f"{ENV_PATCH_FILE}: '{ENV_PATCH_TARGETS}' must be an object")
    macs = targets.get(ENV_PATCH_TARGET_MACS, [])
    if not isinstance(macs, list) or not all(isinstance(m, str) for m in macs):
        raise ValueError(f"{ENV_PATCH_FILE}: '{ENV_PATCH_TARGETS}.{ENV_PATCH_TARGET_MACS}' must be a list of MAC addresses")
    return patch


def is_env_patch_targeted(patch: dict, device_mac: str) -> bool:
    """True when the patch has no MAC list, or lists this device (case-insensitive)."""
    macs = patch.get(ENV_PATCH_TARGETS, {}).get(ENV_PATCH_TARGET_MACS, [])
    if not macs:
        return True
    return str(device_mac).lower() in {mac.lower() for mac in macs}


def format_env_line(key: str, value: str) -> str:
    if any(char in ENV_CHARS_NEEDING_QUOTES for char in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f"{key}={value}"


def apply_env_patch(env_path: Path, patch: dict) -> dict:
    """
    Apply set_if_missing / set / remove to the device app.env in place.

    Comments, order and keys the patch doesn't mention are kept. Every line of
    a key is updated or removed (duplicates included). When something changes,
    the old file is kept as app.env.bak and the new one is written atomically
    with mode 600. Returns the changed key names only, never values.
    """
    lines = env_path.read_text().splitlines()
    key_lines = {}
    for index, line in enumerate(lines):
        key = parse_env_key(line)
        if key:
            key_lines.setdefault(key, []).append(index)

    changes = {"added": [], "updated": [], "removed": []}

    for key in patch.get(ENV_PATCH_REMOVE, []):
        for index in key_lines.pop(key, []):
            lines[index] = None
            if key not in changes["removed"]:
                changes["removed"].append(key)

    for key, value in patch.get(ENV_PATCH_SET, {}).items():
        new_line = format_env_line(key, value)
        if key in key_lines:
            if any(lines[index] != new_line for index in key_lines[key]):
                changes["updated"].append(key)
            for index in key_lines[key]:
                lines[index] = new_line
        else:
            lines.append(new_line)
            key_lines[key] = [len(lines) - 1]
            changes["added"].append(key)

    for key, value in patch.get(ENV_PATCH_SET_IF_MISSING, {}).items():
        if key not in key_lines:
            lines.append(format_env_line(key, value))
            key_lines[key] = [len(lines) - 1]
            changes["added"].append(key)

    if not any(changes.values()):
        return changes

    shutil.copy2(env_path, env_path.with_name(env_path.name + ENV_BACKUP_SUFFIX))
    write_env_lines_atomically(env_path, [line for line in lines if line is not None])
    return {name: sorted(keys) for name, keys in changes.items()}


def restore_env_backup(env_path: Path) -> bool:
    """Put app.env.bak back in place after a failed update. Returns True when restored."""
    backup = env_path.with_name(env_path.name + ENV_BACKUP_SUFFIX)
    if not backup.is_file():
        return False
    os.replace(backup, env_path)
    return True

# ==============================
# Acknowledgement
# ==============================
def publish_ack(client, status: str, **details):
    payload = {"mac_Id": MAC_ADDRESS, "status": status, **details}
    client.publish(ACK_TOPIC, payload=json.dumps(payload), qos=ACK_QOS)

# ==============================
# Restart PM2
# ==============================
def restart_pm2_service():
    try:
        subprocess.run(["pm2", "restart", *PM2_SERVICES_TO_RESTART], check=True)
        print(f"[info] PM2 service  restarted successfully.")
    except subprocess.CalledProcessError as e:
        print(f"[error] Failed to restart PM2 service: {e}")


def restart_agent_service():
    try:
        subprocess.run(["pm2", "restart", PM2_AGENT_SERVICE], check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"[error] Failed to restart the agent ({PM2_AGENT_SERVICE}): {e}")


def schedule_agent_restart():
    """
    Restart the agent after a short delay. The ack is published from this same
    thread's network loop, so waiting for it here would deadlock; the restart
    runs on a timer instead and kills this process once the ack is out.
    """
    threading.Timer(AGENT_RESTART_DELAY_IN_MS / MS_PER_SECOND, restart_agent_service).start()
    print(f"[info] Agent restart ({PM2_AGENT_SERVICE}) scheduled in {AGENT_RESTART_DELAY_IN_MS} ms.")

# ==============================
# MQTT callbacks
# ==============================
def reset_session(session_id: str):
    sessions.pop(session_id, None)

def on_connect(client, userdata, flags, rc, properties=None):
    print("[info] Connected with result code", rc)
    client.subscribe(f"{TOPIC}/+/manifest")
    client.subscribe(f"{TOPIC}/+/chunks/meta/+")
    client.subscribe(f"{TOPIC}/+/chunks/data/+")
    client.subscribe(f"{TOPIC}/+/complete")

def on_message(client, userdata, msg):
    topic_parts = msg.topic.split('/')
    if len(topic_parts) < 3:
        return
    session_id = topic_parts[2]

    if topic_parts[-1] == "manifest":
        manifest = json.loads(msg.payload.decode())
        sessions[session_id] = {
            "manifest": manifest,
            "chunks": {},
        }
        print(f"[info] New session {session_id}, expecting {manifest['total_chunks']} chunks")

    elif "chunks" in topic_parts and topic_parts[-2] == "data":
        index = int(topic_parts[-1])
        sessions[session_id]["chunks"][index] = msg.payload
        print(f"[info] Got chunk {index} for {session_id} ({len(msg.payload)} bytes)")

    elif topic_parts[-1] == "complete":
        complete = json.loads(msg.payload.decode())
        if session_id not in sessions:
            print("[warn] Complete for unknown session")
            return
        sess = sessions[session_id]
        manifest = sess["manifest"]

        if len(sess["chunks"]) != manifest["total_chunks"]:
            print(f"[warn] Missing chunks: {len(sess['chunks'])}/{manifest['total_chunks']}")
            return

        file_bytes = b''.join(sess["chunks"][i] for i in range(manifest["total_chunks"]))

        sha = hashlib.sha256(file_bytes).hexdigest()
        if sha != manifest["sha256"]:
            print("[error] SHA256 mismatch! Aborting deployment.")
            return
        print("[info] SHA256 verified OK.")

        # Prepare staging directory
        if STAGING_DIR.exists():
            shutil.rmtree(STAGING_DIR)
        STAGING_DIR.mkdir(parents=True)

        tar_file = STAGING_DIR / manifest["filename"]
        with open(tar_file, "wb") as f:
            f.write(file_bytes)
        print(f"[info] Saved OTA tarball to {tar_file}")

        # Extract safely into staging
        with tarfile.open(tar_file, "r:gz") as tar:
            safe_extract(tar, STAGING_DIR)
        print(f"[info] Extracted into staging: {STAGING_DIR}")

        # Check that an app.env will exist and that the bundle's env patch is valid before touching anything
        device_env = LIVE_CODE_DIR / DEVICE_ENV_FILE
        try:
            require_app_env_source(STAGING_DIR, LIVE_CODE_DIR)
            env_patch = load_env_patch(STAGING_DIR)
        except (RuntimeError, ValueError) as e:
            print(f"[error] {e}. The current code keeps running.")
            publish_ack(client, ACK_STATUS_FAILED, reason=str(e))
            reset_session(session_id)
            return

        # A device without an app.env gets a copy of the bundle's; a device with one keeps it
        # and takes the env patch (key names only are logged and acked - values may be secrets)
        app_env_created = not device_env.is_file()
        env_patch_status = ENV_PATCH_STATUS_NONE
        env_changes = None
        if app_env_created:
            try:
                LIVE_CODE_DIR.mkdir(parents=True, exist_ok=True)
                create_app_env_from_bundle(STAGING_DIR / DEVICE_ENV_FILE, device_env)
            except OSError as e:
                print(f"[error] Could not create {DEVICE_ENV_FILE}: {e}. The current code keeps running.")
                publish_ack(client, ACK_STATUS_FAILED, reason=f"{DEVICE_ENV_FILE} not created: {e}")
                reset_session(session_id)
                return
            print(f"[info] {DEVICE_ENV_FILE} created from the bundle")
            if env_patch is not None:
                env_patch_status = ENV_PATCH_STATUS_SKIPPED_ENV_CREATED
                print(f"[info] {ENV_PATCH_FILE} skipped, {DEVICE_ENV_FILE} was just created")
        elif env_patch is not None:
            if is_env_patch_targeted(env_patch, MAC_ADDRESS):
                try:
                    env_changes = apply_env_patch(device_env, env_patch)
                except OSError as e:
                    print(f"[error] Could not apply {ENV_PATCH_FILE}: {e}. The current code keeps running.")
                    publish_ack(client, ACK_STATUS_FAILED, reason=f"{ENV_PATCH_FILE} not applied: {e}")
                    reset_session(session_id)
                    return
                env_patch_status = ENV_PATCH_STATUS_APPLIED
                print(f"[info] {ENV_PATCH_FILE} applied to {DEVICE_ENV_FILE}: "
                      f"added={env_changes['added']} updated={env_changes['updated']} removed={env_changes['removed']}")
            else:
                env_patch_status = ENV_PATCH_STATUS_NOT_TARGETED
                print(f"[info] {ENV_PATCH_FILE} does not target this device, {DEVICE_ENV_FILE} unchanged")

        missing_env_keys = find_missing_env_keys(STAGING_DIR, device_env)
        if missing_env_keys:
            print(f"[warn] Device {DEVICE_ENV_FILE} lacks keys listed in {EXAMPLE_ENV_FILE}, "
                  f"the code defaults will be used: {', '.join(missing_env_keys)}")

        # Replace live code; put the old app.env back (or remove the new one) if that fails
        try:
            replace_live_code(STAGING_DIR, LIVE_CODE_DIR)
        except Exception as e:
            if app_env_created:
                device_env.unlink(missing_ok=True)
            env_restored = bool(env_changes and any(env_changes.values())) and restore_env_backup(device_env)
            print(f"[error] Replacing the live code failed: {e}"
                  f"{f' ({DEVICE_ENV_FILE} restored from backup)' if env_restored else ''}")
            publish_ack(client, ACK_STATUS_FAILED, reason=f"replace failed: {e}", env_restored=env_restored)
            reset_session(session_id)
            return

        publish_ack(client, ACK_STATUS_UPDATED, missing_env_keys=missing_env_keys,
                    env_patch=env_patch_status, env_changes=env_changes,
                    app_env_created=app_env_created)

        reset_session(session_id)

        restart_pm2_service()
        schedule_agent_restart()
# ===========================
# Subscription callbacks
# ===========================

# ==============================
# Run
# ==============================

client = paho.Client(client_id="", userdata=None, protocol=paho.MQTTv5)
client.username_pw_set( USER, PASSWORD)
client.on_connect = on_connect
client.on_message = on_message
try:
    client.tls_set(cert_reqs=paho.ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.connect( BROKER, int(PORT))
except Exception as e:
    print(e , traceback.print_exc())
    quit()

client.loop_forever()