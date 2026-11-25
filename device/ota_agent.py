import argparse
import os
import shutil
import tarfile
import time
import json
import hashlib
import platform
import re
import subprocess
import sys
import threading
import traceback
import sqlite3
import uuid
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
ENV_KEY_BROKER_DB_PATH = "BROKER_DB_PATH"
APP_ENV_REQUIRED_KEYS = (ENV_KEY_MQTT_PORT, ENV_KEY_MQTT_USERNAME, ENV_KEY_MQTT_PASSWORD)
AGENT_ENV_REQUIRED_KEYS = (ENV_KEY_MQTT_BASE_TOPIC, ENV_KEY_STAGING_DIR, ENV_KEY_PROD_DIR, ENV_KEY_BROKER_DB_PATH)

BROKER_DB_TABLE = "static_ip_config"
BROKER_DB_COLUMN = "staticIp"

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


def get_broker_from_db(db_path: str):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(f"SELECT {BROKER_DB_COLUMN} FROM {BROKER_DB_TABLE} LIMIT 1;")
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
        raise RuntimeError(f"No broker address found in {BROKER_DB_TABLE} table")
    except Exception as e:
        print(f"[error] Failed to fetch MQTT_BROKER from DB ({db_path}): {e}", file=sys.stderr)
        sys.exit(EXIT_CODE_CONFIG_ERROR)


# Env loading and MQTT client setup happen in the __main__ guard at the bottom
# of this file so a `--selfcheck` subprocess (spawned by the running agent to
# validate a candidate ota_agent.py) short-circuits before touching the live
# broker, DB, or envs. The names below are populated there and referenced by
# the module-level callbacks only once the main loop is running.
APP_ENV = None
AGENT_ENV = None
BROKER = None
PORT = None
USER = None
PASSWORD = None
TOPIC = None

STAGING_DIR = None          # temp extract location for normal OTA
LIVE_CODE_DIR = None        # live code directory on the device

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

# Ack payload discriminator so the server can tell the two pipelines apart.
ACK_KEY_MODULE = "module"
ACK_MODULE_APP = "app"               # normal OTA that replaces LIVE_CODE_DIR
ACK_MODULE_AGENT = "ota_agent"       # agent self-update pipeline
# Which step of the agent-OTA flow failed. Sent only on failures so the server
# can group failures by cause.
ACK_KEY_STAGE = "stage"
ACK_KEY_REASON = "reason"
ACK_STAGE_SHA = "sha_mismatch"
ACK_STAGE_CHUNKS_MISSING = "chunks_missing"
ACK_STAGE_EXTRACT = "extract"
ACK_STAGE_BUNDLE_SHAPE = "bundle_shape"
ACK_STAGE_ENV_VALIDATION = "env_validation"
ACK_STAGE_PROD_DIR_CHECK = "prod_dir_check"
ACK_STAGE_SELFCHECK = "selfcheck"
ACK_STAGE_REPLACE = "replace"
ACK_STAGE_PM2_REACHABLE = "pm2_reachable"
ACK_STAGE_PM2_RESTART = "pm2_restart"
ACK_STAGE_CONCURRENCY = "concurrency"

# Manifest 'targets' block: lets the publisher deploy to a subset of the group.
#   {"mode": "all"}                            -> everyone in the group
#   {"mode": "include", "mac": ["AA:BB:..."]}  -> only these devices
#   {"mode": "exclude", "mac": ["AA:BB:..."]}  -> everyone except these
# A missing 'targets' block means "all" (older publishers).
MANIFEST_KEY_TARGETS = "targets"
TARGETS_KEY_MODE = "mode"
TARGETS_KEY_MAC = "mac"
TARGETS_MODE_ALL = "all"
TARGETS_MODE_INCLUDE = "include"
TARGETS_MODE_EXCLUDE = "exclude"

# MQTT topic segments: <TOPIC>/<session_id>/<segment>[/<data|meta>/<index>]
TOPIC_SEG_MANIFEST = "manifest"
TOPIC_SEG_COMPLETE = "complete"
TOPIC_SEG_CHUNKS = "chunks"
TOPIC_SEG_META = "meta"
TOPIC_SEG_DATA = "data"

PM2_SERVICES_TO_RESTART = ("scheduler", "app", "MQTT_Control_Consumer")
PM2_AGENT_SERVICE = "ota"
# Restarting the agent kills this process, so it waits for the ack to go out first.
AGENT_RESTART_DELAY_IN_MS = 3000
MS_PER_SECOND = 1000

# ==============================
# Agent self-update pipeline
# ==============================
# A separate MQTT topic tree carries updates to the agent itself (ota_agent.py
# and/or agent.env). Keeping it off the normal OTA topic means a routine
# app-code publish cannot accidentally touch the agent, and vice versa.
AGENT_OTA_TOPIC = "OTA_AGENT"
AGENT_OTA_STAGING_DIR = AGENT_DIR / "agent_staging"

# Session kinds — one entry per active session marks which pipeline it belongs
# to, so the concurrency guard can reject an agent-OTA manifest whenever any
# other session is already in flight (per spec: a collision means an accident).
SESSION_KIND_KEY = "kind"
SESSION_KIND_APP = "app"
SESSION_KIND_AGENT = "agent"

# Bundle contents for an agent OTA. Everything is optional, but at least one
# must be present; agent.env and agent_env_patch.json are mutually exclusive
# (one describes a full file, the other a diff — applying both is ambiguous).
AGENT_BUNDLE_SCRIPT = "ota_agent.py"
AGENT_BUNDLE_ENV = "agent.env"
AGENT_BUNDLE_ENV_PATCH = "agent_env_patch.json"

AGENT_SCRIPT_PATH = AGENT_DIR / AGENT_BUNDLE_SCRIPT
AGENT_SCRIPT_BAK = AGENT_SCRIPT_PATH.with_name(AGENT_BUNDLE_SCRIPT + ENV_BACKUP_SUFFIX)
AGENT_ENV_BAK = AGENT_ENV_PATH.with_name(AGENT_ENV_PATH.name + ENV_BACKUP_SUFFIX)

# Selfcheck: spawn the candidate ota_agent.py with --selfcheck so it validates
# imports, both env files, and MQTT connectivity before we commit the swap. A
# distinct client_id suffix avoids kicking the running agent off the broker
# via MQTT 5 take-over semantics.
SELFCHECK_FLAG = "--selfcheck"
SELFCHECK_AGENT_ENV_FLAG = "--selfcheck-agent-env"
SELFCHECK_APP_ENV_FLAG = "--selfcheck-app-env"
SELFCHECK_CLIENT_ID_SUFFIX = "_selfcheck"
SELFCHECK_TIMEOUT_SECS = 15
SELFCHECK_CONNECT_TIMEOUT_SECS = 10
SELFCHECK_STDERR_MAX_CHARS = 300  # bounds what we put into the failure reason

# Non-zero exit codes from the selfcheck subprocess. The parent maps these to
# reason strings in the ack so the server sees which check failed.
SELFCHECK_EXIT_OK = 0
SELFCHECK_EXIT_AGENT_ENV_MISSING = 10
SELFCHECK_EXIT_AGENT_ENV_INVALID = 11
SELFCHECK_EXIT_APP_ENV_MISSING = 20
SELFCHECK_EXIT_APP_ENV_INVALID = 21
SELFCHECK_EXIT_BROKER_LOOKUP = 30
SELFCHECK_EXIT_MQTT_CONNECT = 31
SELFCHECK_EXIT_MQTT_ERROR = 32
SELFCHECK_EXIT_SPAWN_TIMEOUT = 97
SELFCHECK_EXIT_SPAWN_ERROR = 98

sessions = {}
skipped_sessions = set()

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


def is_session_targeted(targets, device_mac: str) -> bool:
    """
    True when the manifest's 'targets' block includes this device.

    An absent block or mode='all' means every device. Unknown modes fail
    closed, so a malformed manifest never installs on anything.
    """
    if not isinstance(targets, dict):
        return True
    mode = targets.get(TARGETS_KEY_MODE, TARGETS_MODE_ALL)
    if mode == TARGETS_MODE_ALL:
        return True
    mac_list = targets.get(TARGETS_KEY_MAC, [])
    if not isinstance(mac_list, list):
        return False
    mac_set = {str(mac).lower() for mac in mac_list}
    device_mac_lower = str(device_mac).lower()
    if mode == TARGETS_MODE_INCLUDE:
        return device_mac_lower in mac_set
    if mode == TARGETS_MODE_EXCLUDE:
        return device_mac_lower not in mac_set
    return False


def format_env_line(key: str, value: str) -> str:
    if any(char in ENV_CHARS_NEEDING_QUOTES for char in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f"{key}={value}"


def apply_env_patch(env_path: Path, patch: dict):
    """
    Apply set_if_missing / set / remove to the device app.env in place.

    Comments, order and keys the patch doesn't mention are kept. Every line of
    a key is updated or removed (duplicates included). When something changes,
    the old file is kept as app.env.bak and the new one is written atomically
    with mode 600. Returns the changed key names only, never values — or None
    when the patch had no effect, so callers can use a plain truthiness check.
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
        return None

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


def check_pm2_service_reachable(service: str):
    """
    Return (ok, detail). True when `pm2 describe <service>` exits 0, meaning
    the pm2 daemon is up and knows about this service. Done before we commit
    the swap so we never leave the device with new files and no way to run.
    """
    try:
        result = subprocess.run(
            ["pm2", "describe", service],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, f"pm2 describe exit {result.returncode}"
        return True, ""
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"pm2 describe failed: {type(e).__name__}"


def schedule_agent_restart_with_failure_ack(client):
    """
    Agent-OTA variant of the restart: on failure (pm2 call errors or exits
    with a positive status), emit a correction ack with stage=pm2_restart.

    pm2 restart stops this process with SIGINT as part of a successful
    restart. The pm2 CLI we spawn here is detached into its own process group
    so that signal does not sweep it up; a negative returncode (signal death)
    is still treated as success in case the detach does not take effect.
    """
    def restart():
        try:
            proc = subprocess.run(
                ["pm2", "restart", PM2_AGENT_SERVICE],
                timeout=30, start_new_session=True,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            reason = f"pm2 restart failed: {type(e).__name__}"
        else:
            if proc.returncode <= 0:
                return
            reason = f"pm2 restart exit {proc.returncode}"
        print(f"[error] {reason}")
        publish_ack(
            client, ACK_STATUS_FAILED,
            **{
                ACK_KEY_MODULE: ACK_MODULE_AGENT,
                ACK_KEY_STAGE: ACK_STAGE_PM2_RESTART,
                ACK_KEY_REASON: reason,
            },
        )
    threading.Timer(AGENT_RESTART_DELAY_IN_MS / MS_PER_SECOND, restart).start()
    print(f"[info] Agent restart ({PM2_AGENT_SERVICE}) scheduled in {AGENT_RESTART_DELAY_IN_MS} ms.")


# ==============================
# Agent OTA pipeline
# ==============================
def publish_agent_ack_failed(client, stage: str, reason: str, **extras):
    """Agent-OTA failure ack: stage and reason are mandatory, module is fixed."""
    publish_ack(
        client, ACK_STATUS_FAILED,
        **{ACK_KEY_MODULE: ACK_MODULE_AGENT, ACK_KEY_STAGE: stage, ACK_KEY_REASON: reason},
        **extras,
    )


def prune_agent_backups():
    """
    Remove any .bak files left by previous agent-OTAs. Called at the start of
    every new agent-OTA so the backups on disk always reflect the state
    immediately before the most recent update, not some older ancestor.
    """
    for bak in (AGENT_SCRIPT_BAK, AGENT_ENV_BAK):
        if bak.is_file():
            try:
                bak.unlink()
                print(f"[info] Removed stale backup {bak.name}")
            except OSError as e:
                print(f"[warn] Could not remove stale backup {bak}: {e}")


def load_agent_env_patch(patch_path: Path):
    """
    Load and validate the bundle's agent_env_patch.json, reusing the same
    shape rules as the app-env patch. Raises ValueError on any violation so a
    bad patch aborts before anything on disk is touched.
    """
    try:
        patch = json.loads(patch_path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{AGENT_BUNDLE_ENV_PATCH} is not valid JSON: {e}") from e
    if not isinstance(patch, dict):
        raise ValueError(f"{AGENT_BUNDLE_ENV_PATCH} must be a JSON object")
    patch = {key: value for key, value in patch.items() if not str(key).startswith(ENV_PATCH_NOTE_PREFIX)}
    unknown = set(patch) - ENV_PATCH_SECTIONS
    if unknown:
        raise ValueError(f"{AGENT_BUNDLE_ENV_PATCH}: unknown sections {sorted(unknown)}")
    _validate_env_mapping(patch, ENV_PATCH_SET_IF_MISSING)
    _validate_env_mapping(patch, ENV_PATCH_SET)
    removed = patch.get(ENV_PATCH_REMOVE, [])
    if not isinstance(removed, list) or not all(isinstance(k, str) and ENV_KEY_PATTERN.match(k) for k in removed):
        raise ValueError(f"{AGENT_BUNDLE_ENV_PATCH}: '{ENV_PATCH_REMOVE}' must be a list of key names")
    written = set(patch.get(ENV_PATCH_SET, {})) | set(patch.get(ENV_PATCH_SET_IF_MISSING, {}))
    conflicts = written & set(removed)
    if conflicts:
        raise ValueError(f"{AGENT_BUNDLE_ENV_PATCH}: keys both set and removed: {sorted(conflicts)}")
    return patch


def materialise_effective_agent_env(staging: Path, new_env: Path, env_patch, has_env: bool):
    """
    Produce the agent.env the new ota_agent.py would see on next boot so the
    selfcheck subprocess can validate it. Full replacement -> copy the
    bundle's. Patch -> apply the patch to a shadow of the current file. No
    change -> a copy of the current file.
    """
    effective = staging / "agent.env.effective"
    if has_env:
        shutil.copyfile(new_env, effective)
    else:
        shutil.copyfile(AGENT_ENV_PATH, effective)
        if env_patch is not None:
            apply_env_patch(effective, env_patch)
            # apply_env_patch leaves a .bak next to the shadow; the shadow is
            # transient so we drop it to keep the staging directory clean.
            shadow_bak = effective.with_name(effective.name + ENV_BACKUP_SUFFIX)
            if shadow_bak.is_file():
                shadow_bak.unlink()
    return effective


def _selfcheck_reason_from(exit_code: int, stderr_tail: str) -> str:
    """Map a non-zero exit code to a short cause, appending the last structured line if present."""
    mapping = {
        SELFCHECK_EXIT_AGENT_ENV_MISSING: "agent.env missing",
        SELFCHECK_EXIT_AGENT_ENV_INVALID: "agent.env missing required keys",
        SELFCHECK_EXIT_APP_ENV_MISSING: "prod/app.env missing",
        SELFCHECK_EXIT_APP_ENV_INVALID: "prod/app.env missing required keys",
        SELFCHECK_EXIT_BROKER_LOOKUP: "broker lookup failed",
        SELFCHECK_EXIT_MQTT_CONNECT: "mqtt connect failed",
        SELFCHECK_EXIT_MQTT_ERROR: "mqtt setup error",
        SELFCHECK_EXIT_SPAWN_TIMEOUT: "selfcheck timed out",
        SELFCHECK_EXIT_SPAWN_ERROR: "selfcheck could not be spawned",
    }
    base = mapping.get(exit_code, f"selfcheck exit {exit_code}")
    tail = ""
    for line in reversed(stderr_tail.splitlines()):
        if line.startswith("[selfcheck:"):
            tail = line.strip()
            break
    return f"{base} ({tail})" if tail else base


def run_candidate_selfcheck(script_path: Path, agent_env_path: Path, app_env_path: Path):
    """
    Spawn the candidate ota_agent.py with --selfcheck. Returns (exit_code, reason).
    """
    try:
        result = subprocess.run(
            [
                sys.executable, str(script_path), SELFCHECK_FLAG,
                SELFCHECK_AGENT_ENV_FLAG, str(agent_env_path),
                SELFCHECK_APP_ENV_FLAG, str(app_env_path),
            ],
            capture_output=True, text=True, timeout=SELFCHECK_TIMEOUT_SECS,
        )
        stderr_tail = (result.stderr or "")[-SELFCHECK_STDERR_MAX_CHARS:]
        if result.returncode == SELFCHECK_EXIT_OK:
            return SELFCHECK_EXIT_OK, ""
        return result.returncode, _selfcheck_reason_from(result.returncode, stderr_tail)
    except subprocess.TimeoutExpired:
        return SELFCHECK_EXIT_SPAWN_TIMEOUT, _selfcheck_reason_from(SELFCHECK_EXIT_SPAWN_TIMEOUT, "")
    except OSError as e:
        return SELFCHECK_EXIT_SPAWN_ERROR, f"selfcheck spawn error: {type(e).__name__}"


def swap_agent_script(new_script: Path):
    """Back up the current ota_agent.py and atomically replace it with the candidate."""
    shutil.copy2(AGENT_SCRIPT_PATH, AGENT_SCRIPT_BAK)
    tmp = AGENT_SCRIPT_PATH.with_name(AGENT_SCRIPT_PATH.name + ENV_TMP_SUFFIX)
    tmp.write_bytes(new_script.read_bytes())
    os.chmod(tmp, 0o755)
    os.replace(tmp, AGENT_SCRIPT_PATH)


def swap_agent_env(new_env: Path):
    """Back up the current agent.env and atomically replace it with the bundle's."""
    shutil.copy2(AGENT_ENV_PATH, AGENT_ENV_BAK)
    write_env_lines_atomically(AGENT_ENV_PATH, new_env.read_text().splitlines())


def restore_agent_backups(script_was_swapped: bool, env_was_swapped: bool):
    """Put .bak files back after a failed partial swap. Best-effort."""
    if script_was_swapped and AGENT_SCRIPT_BAK.is_file():
        try:
            os.replace(AGENT_SCRIPT_BAK, AGENT_SCRIPT_PATH)
        except OSError as e:
            print(f"[error] Could not restore ota_agent.py from backup: {e}")
    if env_was_swapped and AGENT_ENV_BAK.is_file():
        try:
            os.replace(AGENT_ENV_BAK, AGENT_ENV_PATH)
        except OSError as e:
            print(f"[error] Could not restore agent.env from backup: {e}")


def handle_agent_ota_complete(client, session_id: str):
    """
    Full agent-OTA pipeline from a verified-complete session: SHA check,
    extract, bundle-shape and env validation, PROD_DIR check, backup pruning,
    selfcheck of the candidate, pm2 reachability check, atomic swap, ack,
    restart-with-failure-ack. Every failure path keeps the current code
    running and emits an ack with stage+reason.
    """
    sess = sessions.get(session_id)
    if not sess:
        return
    manifest = sess["manifest"]

    if len(sess["chunks"]) != manifest["total_chunks"]:
        publish_agent_ack_failed(
            client, ACK_STAGE_CHUNKS_MISSING,
            f"missing chunks: {len(sess['chunks'])}/{manifest['total_chunks']}",
        )
        reset_session(session_id)
        return

    file_bytes = b"".join(sess["chunks"][i] for i in range(manifest["total_chunks"]))
    sha = hashlib.sha256(file_bytes).hexdigest()
    if sha != manifest["sha256"]:
        publish_agent_ack_failed(client, ACK_STAGE_SHA, "sha256 mismatch")
        reset_session(session_id)
        return

    try:
        if AGENT_OTA_STAGING_DIR.exists():
            shutil.rmtree(AGENT_OTA_STAGING_DIR)
        AGENT_OTA_STAGING_DIR.mkdir(parents=True)
        tar_file = AGENT_OTA_STAGING_DIR / manifest["filename"]
        tar_file.write_bytes(file_bytes)
        with tarfile.open(tar_file, "r:gz") as tar:
            safe_extract(tar, AGENT_OTA_STAGING_DIR)
    except Exception as e:
        publish_agent_ack_failed(client, ACK_STAGE_EXTRACT, f"extract failed: {type(e).__name__}: {e}")
        reset_session(session_id)
        return

    new_script = AGENT_OTA_STAGING_DIR / AGENT_BUNDLE_SCRIPT
    new_env = AGENT_OTA_STAGING_DIR / AGENT_BUNDLE_ENV
    new_env_patch = AGENT_OTA_STAGING_DIR / AGENT_BUNDLE_ENV_PATCH
    has_script = new_script.is_file()
    has_env = new_env.is_file()
    has_env_patch = new_env_patch.is_file()

    if has_env and has_env_patch:
        publish_agent_ack_failed(
            client, ACK_STAGE_BUNDLE_SHAPE,
            f"{AGENT_BUNDLE_ENV} and {AGENT_BUNDLE_ENV_PATCH} are mutually exclusive",
        )
        reset_session(session_id)
        return
    if not (has_script or has_env or has_env_patch):
        publish_agent_ack_failed(client, ACK_STAGE_BUNDLE_SHAPE, "bundle has nothing to apply")
        reset_session(session_id)
        return

    env_patch = None
    if has_env_patch:
        try:
            env_patch = load_agent_env_patch(new_env_patch)
        except ValueError as e:
            publish_agent_ack_failed(client, ACK_STAGE_ENV_VALIDATION, str(e))
            reset_session(session_id)
            return

    prod_app_env = LIVE_CODE_DIR / DEVICE_ENV_FILE
    if not LIVE_CODE_DIR.is_dir() or not prod_app_env.is_file():
        publish_agent_ack_failed(
            client, ACK_STAGE_PROD_DIR_CHECK,
            f"PROD_DIR missing or has no {DEVICE_ENV_FILE}: {LIVE_CODE_DIR}",
        )
        reset_session(session_id)
        return

    # Clean out any .bak from a previous agent-OTA before we create fresh ones.
    prune_agent_backups()

    try:
        effective_agent_env = materialise_effective_agent_env(
            AGENT_OTA_STAGING_DIR, new_env, env_patch, has_env,
        )
    except Exception as e:
        publish_agent_ack_failed(
            client, ACK_STAGE_ENV_VALIDATION,
            f"effective agent.env preparation failed: {type(e).__name__}: {e}",
        )
        reset_session(session_id)
        return

    # When the bundle has no new script, we validate the effective env against
    # the current script's expectations — enough to catch missing keys or bad
    # credentials; it does not prove the current script will tolerate new
    # semantics, but there isn't one to validate against.
    selfcheck_script = new_script if has_script else AGENT_SCRIPT_PATH
    rc, reason = run_candidate_selfcheck(selfcheck_script, effective_agent_env, prod_app_env)
    if rc != SELFCHECK_EXIT_OK:
        publish_agent_ack_failed(client, ACK_STAGE_SELFCHECK, reason)
        reset_session(session_id)
        return

    pm2_ok, pm2_detail = check_pm2_service_reachable(PM2_AGENT_SERVICE)
    if not pm2_ok:
        publish_agent_ack_failed(client, ACK_STAGE_PM2_REACHABLE, pm2_detail)
        reset_session(session_id)
        return

    script_swapped = False
    env_swapped = False
    env_patch_changes = None
    try:
        if has_script:
            swap_agent_script(new_script)
            script_swapped = True
        if has_env:
            swap_agent_env(new_env)
            env_swapped = True
        elif env_patch is not None:
            env_patch_changes = apply_env_patch(AGENT_ENV_PATH, env_patch)
            env_swapped = env_patch_changes is not None
    except Exception as e:
        restore_agent_backups(script_swapped, env_swapped)
        publish_agent_ack_failed(
            client, ACK_STAGE_REPLACE,
            f"swap failed: {type(e).__name__}: {e}",
        )
        reset_session(session_id)
        return

    publish_ack(
        client, ACK_STATUS_UPDATED,
        **{
            ACK_KEY_MODULE: ACK_MODULE_AGENT,
            "script_replaced": has_script,
            "env_replaced": has_env,
            "env_patch_applied": has_env_patch,
            "env_changes": env_patch_changes,
        },
    )

    reset_session(session_id)
    schedule_agent_restart_with_failure_ack(client)




# ==============================
# Selfcheck (runs in the candidate agent's own process)
# ==============================
def _selfcheck_log(step: str, ok: bool, detail: str = ""):
    """
    Emit one line per selfcheck step. All output is structured as
    `[selfcheck:<step>] ok|fail <detail>` so the parent can grep for failures
    without the risk of pulling a credential value out of a random stderr line.
    """
    stream = sys.stdout if ok else sys.stderr
    status = "ok" if ok else "fail"
    message = f"[selfcheck:{step}] {status}"
    if detail:
        message = f"{message} {detail}"
    print(message, file=stream, flush=True)


def _selfcheck_env(step: str, env_path: Path, required_keys) -> int:
    """Return 0 when env_path exists and defines every required key with a non-empty value."""
    if not env_path.is_file():
        _selfcheck_log(step, False, f"file missing: {env_path}")
        return SELFCHECK_EXIT_AGENT_ENV_MISSING if step == "agent_env" else SELFCHECK_EXIT_APP_ENV_MISSING
    values = dotenv_values(env_path)
    missing = [key for key in required_keys if not values.get(key)]
    if missing:
        _selfcheck_log(step, False, f"missing keys: {','.join(missing)}")
        return SELFCHECK_EXIT_AGENT_ENV_INVALID if step == "agent_env" else SELFCHECK_EXIT_APP_ENV_INVALID
    _selfcheck_log(step, True)
    return SELFCHECK_EXIT_OK


def _selfcheck_mqtt_connect(broker: str, port: int, user: str, password: str, client_id: str) -> int:
    """Attempt an MQTT connect-and-disconnect with the given credentials. No subscribe, no publish to shared topics."""
    state = {"rc": None, "connected": False}

    def _on_connect(_client, _userdata, _flags, rc, _properties=None):
        state["rc"] = rc
        state["connected"] = (rc == 0)

    try:
        client = paho.Client(client_id=client_id, userdata=None, protocol=paho.MQTTv5)
        client.username_pw_set(user, password)
        client.on_connect = _on_connect
        client.tls_set(cert_reqs=paho.ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.connect(broker, port)
    except Exception as e:
        _selfcheck_log("mqtt_connect", False, f"setup error: {type(e).__name__}")
        return SELFCHECK_EXIT_MQTT_ERROR

    deadline = time.time() + SELFCHECK_CONNECT_TIMEOUT_SECS
    while time.time() < deadline and not state["connected"]:
        client.loop(timeout=0.5)

    if not state["connected"]:
        _selfcheck_log("mqtt_connect", False, f"not connected, rc={state['rc']}")
        try:
            client.disconnect()
        except Exception:
            pass
        return SELFCHECK_EXIT_MQTT_CONNECT

    try:
        client.disconnect()
    except Exception:
        pass
    _selfcheck_log("mqtt_connect", True)
    return SELFCHECK_EXIT_OK


def run_selfcheck(argv) -> int:
    """
    Candidate-agent entrypoint for `--selfcheck`. The running agent spawns
    this on the proposed new ota_agent.py with the effective env paths. We
    exit 0 only when imports, both env files, and MQTT connectivity all pass.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(SELFCHECK_FLAG, action="store_true")
    parser.add_argument(SELFCHECK_AGENT_ENV_FLAG, dest="agent_env", required=True)
    parser.add_argument(SELFCHECK_APP_ENV_FLAG, dest="app_env", required=True)
    args, _ = parser.parse_known_args(argv[1:])

    # Imports reaching this point means every top-of-file import resolved.
    _selfcheck_log("imports", True)

    agent_env_path = Path(args.agent_env)
    app_env_path = Path(args.app_env)

    rc = _selfcheck_env("agent_env", agent_env_path, AGENT_ENV_REQUIRED_KEYS)
    if rc != SELFCHECK_EXIT_OK:
        return rc
    rc = _selfcheck_env("app_env", app_env_path, APP_ENV_REQUIRED_KEYS)
    if rc != SELFCHECK_EXIT_OK:
        return rc

    app_values = dotenv_values(app_env_path)
    agent_values = dotenv_values(agent_env_path)
    broker_db_path = agent_values.get(ENV_KEY_BROKER_DB_PATH)
    if not broker_db_path:
        _selfcheck_log("broker_lookup", False, f"{ENV_KEY_BROKER_DB_PATH} not set in agent.env")
        return SELFCHECK_EXIT_BROKER_LOOKUP
    try:
        broker = get_broker_from_db(broker_db_path)
    except SystemExit:
        _selfcheck_log("broker_lookup", False, "db lookup failed")
        return SELFCHECK_EXIT_BROKER_LOOKUP
    _selfcheck_log("broker_lookup", True)

    port = int(app_values[ENV_KEY_MQTT_PORT])
    user = app_values[ENV_KEY_MQTT_USERNAME]
    password = app_values[ENV_KEY_MQTT_PASSWORD]
    client_id = f"{get_ethernet_mac()}{SELFCHECK_CLIENT_ID_SUFFIX}"

    return _selfcheck_mqtt_connect(broker, port, user, password, client_id)


# ==============================
# MQTT callbacks
# ==============================
def reset_session(session_id: str):
    sessions.pop(session_id, None)
    skipped_sessions.discard(session_id)

def on_connect(client, userdata, flags, rc, properties=None):
    print("[info] Connected with result code", rc)
    for root in (TOPIC, AGENT_OTA_TOPIC):
        client.subscribe(f"{root}/+/{TOPIC_SEG_MANIFEST}")
        client.subscribe(f"{root}/+/{TOPIC_SEG_CHUNKS}/{TOPIC_SEG_META}/+")
        client.subscribe(f"{root}/+/{TOPIC_SEG_CHUNKS}/{TOPIC_SEG_DATA}/+")
        client.subscribe(f"{root}/+/{TOPIC_SEG_COMPLETE}")

def on_message(client, userdata, msg):
    # Topic layouts (session_id is a UUID from the publisher):
    #   <root>/<session_id>/manifest
    #   <root>/<session_id>/complete
    #   <root>/<session_id>/chunks/<meta|data>/<index>
    # The root picks the pipeline (TOPIC -> normal app OTA, AGENT_OTA_TOPIC
    # -> agent self-update). Session id is the segment right above the suffix:
    # one back for manifest/complete, three back for chunks.
    if msg.topic.startswith(AGENT_OTA_TOPIC + "/"):
        kind = SESSION_KIND_AGENT
    elif msg.topic.startswith(TOPIC + "/"):
        kind = SESSION_KIND_APP
    else:
        return
    topic_parts = msg.topic.split('/')

    if topic_parts[-1] in (TOPIC_SEG_MANIFEST, TOPIC_SEG_COMPLETE) and len(topic_parts) >= 2:
        session_id = topic_parts[-2]
    elif len(topic_parts) >= 4 and topic_parts[-3] == TOPIC_SEG_CHUNKS:
        session_id = topic_parts[-4]
    else:
        return

    if topic_parts[-1] == TOPIC_SEG_MANIFEST:
        try:
            manifest = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[warn] Dropping malformed manifest on {msg.topic}: {e}")
            return
        if not is_session_targeted(manifest.get(MANIFEST_KEY_TARGETS), MAC_ADDRESS):
            print(f"[info] Session {session_id} does not target this device, skipping")
            skipped_sessions.add(session_id)
            return
        # Agent-OTA refuses to start while any other session is in flight: a
        # collision between pipelines is treated as an accident, and the
        # self-update loses (publisher will retry).
        if kind == SESSION_KIND_AGENT and sessions:
            other = next(iter(sessions.values()))
            print(f"[warn] Agent-OTA manifest arrived while {other.get(SESSION_KIND_KEY)} session is active, rejecting")
            publish_agent_ack_failed(
                client, ACK_STAGE_CONCURRENCY,
                f"another session in progress ({other.get(SESSION_KIND_KEY)})",
            )
            skipped_sessions.add(session_id)
            return
        # App-OTA does not reject on collision (the publisher may legitimately
        # retry a manifest with a fresh session_id), but two live app sessions
        # racing to replace LIVE_CODE_DIR is a last-writer-wins situation that
        # the operator should see in the logs.
        if kind == SESSION_KIND_APP and sessions:
            active_ids = [sid for sid in sessions if sid != session_id]
            if active_ids:
                print(f"[warn] App-OTA manifest {session_id} arrived while session(s) {active_ids} are still in flight; last to complete wins")
        sessions[session_id] = {
            "manifest": manifest,
            "chunks": {},
            SESSION_KIND_KEY: kind,
        }
        print(f"[info] New {kind} session {session_id}, expecting {manifest['total_chunks']} chunks")

    elif TOPIC_SEG_CHUNKS in topic_parts and topic_parts[-2] == TOPIC_SEG_DATA:
        if session_id not in sessions:
            return
        index = int(topic_parts[-1])
        sessions[session_id]["chunks"][index] = msg.payload
        print(f"[info] Got chunk {index} for {session_id} ({len(msg.payload)} bytes)")

    elif topic_parts[-1] == TOPIC_SEG_COMPLETE:
        if session_id in skipped_sessions:
            skipped_sessions.discard(session_id)
            return
        try:
            json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[warn] Dropping malformed complete on {msg.topic}: {e}")
            reset_session(session_id)
            return
        if session_id not in sessions:
            print("[warn] Complete for unknown session")
            return
        if sessions[session_id].get(SESSION_KIND_KEY) == SESSION_KIND_AGENT:
            handle_agent_ota_complete(client, session_id)
            return
        session = sessions[session_id]
        manifest = session["manifest"]
        received_chunks = session["chunks"]

        if len(received_chunks) != manifest["total_chunks"]:
            print(f"[warn] Missing chunks: {len(received_chunks)}/{manifest['total_chunks']}")
            reset_session(session_id)
            return

        file_bytes = b''.join(received_chunks[i] for i in range(manifest["total_chunks"]))

        sha = hashlib.sha256(file_bytes).hexdigest()
        if sha != manifest["sha256"]:
            print("[error] SHA256 mismatch! Aborting deployment.")
            reset_session(session_id)
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
            publish_ack(client, ACK_STATUS_FAILED,
                        **{ACK_KEY_MODULE: ACK_MODULE_APP, ACK_KEY_REASON: str(e)})
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
                publish_ack(client, ACK_STATUS_FAILED,
                            **{ACK_KEY_MODULE: ACK_MODULE_APP,
                               ACK_KEY_REASON: f"{DEVICE_ENV_FILE} not created: {e}"})
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
                    publish_ack(client, ACK_STATUS_FAILED,
                                **{ACK_KEY_MODULE: ACK_MODULE_APP,
                                   ACK_KEY_REASON: f"{ENV_PATCH_FILE} not applied: {e}"})
                    reset_session(session_id)
                    return
                env_patch_status = ENV_PATCH_STATUS_APPLIED
                if env_changes is not None:
                    print(f"[info] {ENV_PATCH_FILE} applied to {DEVICE_ENV_FILE}: "
                          f"added={env_changes['added']} updated={env_changes['updated']} removed={env_changes['removed']}")
                else:
                    print(f"[info] {ENV_PATCH_FILE} applied to {DEVICE_ENV_FILE}: no changes")
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
            env_restored = env_changes is not None and restore_env_backup(device_env)
            print(f"[error] Replacing the live code failed: {e}"
                  f"{f' ({DEVICE_ENV_FILE} restored from backup)' if env_restored else ''}")
            publish_ack(client, ACK_STATUS_FAILED,
                        **{ACK_KEY_MODULE: ACK_MODULE_APP,
                           ACK_KEY_REASON: f"replace failed: {e}"},
                        env_restored=env_restored)
            reset_session(session_id)
            return

        publish_ack(client, ACK_STATUS_UPDATED,
                    **{ACK_KEY_MODULE: ACK_MODULE_APP},
                    missing_env_keys=missing_env_keys,
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
if __name__ == "__main__":
    # The agent spawns itself with --selfcheck to validate a candidate
    # ota_agent.py before committing the swap. That path must not touch the
    # live broker, envs, or the main loop, so it exits here.
    if SELFCHECK_FLAG in sys.argv:
        sys.exit(run_selfcheck(sys.argv))

    APP_ENV = load_required_env(APP_ENV_PATH, APP_ENV_REQUIRED_KEYS)
    AGENT_ENV = load_required_env(AGENT_ENV_PATH, AGENT_ENV_REQUIRED_KEYS)

    BROKER = get_broker_from_db(AGENT_ENV[ENV_KEY_BROKER_DB_PATH])
    PORT = APP_ENV[ENV_KEY_MQTT_PORT]
    USER = APP_ENV[ENV_KEY_MQTT_USERNAME]
    PASSWORD = APP_ENV[ENV_KEY_MQTT_PASSWORD]
    TOPIC = AGENT_ENV[ENV_KEY_MQTT_BASE_TOPIC]

    STAGING_DIR = Path(AGENT_ENV[ENV_KEY_STAGING_DIR])
    LIVE_CODE_DIR = Path(AGENT_ENV[ENV_KEY_PROD_DIR])

    client = paho.Client(client_id="", userdata=None, protocol=paho.MQTTv5)
    client.username_pw_set(USER, PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.tls_set(cert_reqs=paho.ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.connect(BROKER, int(PORT))
    except Exception as e:
        print(f"[error] MQTT connect failed: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(EXIT_CODE_CONFIG_ERROR)

    client.loop_forever()