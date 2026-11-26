#!/usr/bin/env python3
"""
Agent self-update publisher — high-stakes pipeline that ships a new
ota_agent.py and/or agent.env to the device fleet over the OTA_AGENT MQTT
topic.

A bad release on this pipeline requires physical access to recover, so this
tool refuses to publish until an operator has
  (1) authenticated with the agent-OTA password, and
  (2) typed the full risk-acknowledgement phrase.

Every invocation (successful or aborted) is appended to agent_publisher.audit.log.
"""
import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as paho
from dotenv import dotenv_values

from device_targeting import (
    TargetingError,
    build_manifest_targets,
    fetch_devices,
    load_targets_config,
    resolve_targets,
    write_resolved_file,
)
from ack_capture import DeploymentAckCollector, DEFAULT_ACK_WAIT_SECONDS

# =========================
# Paths and layout
# =========================
SERVER_DIR = Path(__file__).resolve().parent
PUBLISHER_ENV_PATH = SERVER_DIR / "publisher.env"               # shared with the normal publisher: MQTT + Mongo
AGENT_PASSWORD_CONFIG_PATH = SERVER_DIR / "agent_publisher.config.json"
AGENT_TARGETS_CONFIG_PATH = SERVER_DIR / "agent_targets.config.json"
AGENT_TARGETS_RESOLVED_PATH = SERVER_DIR / "agent_targets.resolved.json"
AUDIT_LOG_PATH = SERVER_DIR / "agent_publisher.audit.log"

# Must match the device agent's AGENT_OTA_TOPIC constant.
AGENT_OTA_TOPIC = "OTA_AGENT"

# Bundle entry names the device agent recognises.
AGENT_BUNDLE_SCRIPT = "ota_agent.py"
AGENT_BUNDLE_ENV = "agent.env"
AGENT_BUNDLE_ENV_PATCH = "agent_env_patch.json"

# REQUIRED_ENV_KEYS is defined below once the PUB_ENV_KEY_* constants exist.

# =========================
# Password gate (PBKDF2, stdlib only)
# =========================
# We store only a salted PBKDF2 hash on disk. Someone who reads the config
# file must brute-force the password to use it. Rotating the password just
# means deleting the file and running the tool once to re-prompt.
PASSWORD_KDF = "pbkdf2_sha256"
PASSWORD_HASH_ALGO = "sha256"              # pbkdf2_hmac first arg
PASSWORD_ITERATIONS = 200_000
PASSWORD_SALT_BYTES = 16
PASSWORD_KEY_BYTES = 32
PASSWORD_MIN_LENGTH = 8                    # low floor; raise for stronger policy
PASSWORD_ENCODING = "utf-8"

# Keys inside agent_publisher.config.json.
PASSWORD_CONFIG_KEY_KDF = "kdf"
PASSWORD_CONFIG_KEY_ITERATIONS = "iterations"
PASSWORD_CONFIG_KEY_SALT_HEX = "salt_hex"
PASSWORD_CONFIG_KEY_HASH_HEX = "hash_hex"
PASSWORD_CONFIG_KEY_CREATED_AT = "created_at"
PASSWORD_CONFIG_REQUIRED_KEYS = (
    PASSWORD_CONFIG_KEY_KDF, PASSWORD_CONFIG_KEY_ITERATIONS,
    PASSWORD_CONFIG_KEY_SALT_HEX, PASSWORD_CONFIG_KEY_HASH_HEX,
)

PASSWORD_CONFIG_FILE_MODE = 0o600
TMP_SUFFIX = ".tmp"

# =========================
# Risk acknowledgement
# =========================
RISK_PROMPT_PHRASE = "I UNDERSTAND"
RISK_PROMPT_TEXT = (
    "\n"
    "================================================================\n"
    "  AGENT-OTA PUBLISHER — HIGH-RISK OPERATION\n"
    "================================================================\n"
    "  You are about to replace the OTA agent itself (ota_agent.py)\n"
    "  and/or its environment file (agent.env) on the devices below.\n"
    "\n"
    "  A broken release on this pipeline:\n"
    "    - cannot be rolled back via a future OTA (the OTA client is\n"
    "      what this ships)\n"
    "    - requires PHYSICAL ACCESS to every affected device to\n"
    "      recover\n"
    "    - will interrupt all other device services while the agent\n"
    "      restarts\n"
    "\n"
    "  The device-side agent runs a `--selfcheck` subprocess before\n"
    "  it commits the swap; that catches syntax errors, missing env\n"
    "  keys, and broker unreachability. It does NOT catch runtime\n"
    "  behaviour bugs.\n"
    "================================================================\n"
)

# =========================
# MQTT publish settings
# =========================
DEFAULT_CHUNK_SIZE_BYTES = 256 * 1024          # 256 KB; most brokers default-limit near here
DEFAULT_QOS = 1
RETAIN_MANIFEST = False
MQTT_KEEPALIVE_SECS = 60

# Size of each disk read when hashing the archive.
SHA256_READ_BUFFER_BYTES = 1_048_576           # 1 MiB

# Group label the ack collector stamps onto report filenames.
PUBLISH_GROUP_AGENT = "ota_agent"

# =========================
# Manifest payload keys (must match the device's MANIFEST_KEY_* constants).
# =========================
MANIFEST_KEY_TYPE = "type"
MANIFEST_KEY_SESSION_ID = "session_id"
MANIFEST_KEY_FILENAME = "filename"
MANIFEST_KEY_SIZE_BYTES = "size_bytes"
MANIFEST_KEY_SHA256 = "sha256"
MANIFEST_KEY_CHUNK_SIZE = "chunk_size"
MANIFEST_KEY_TOTAL_CHUNKS = "total_chunks"
MANIFEST_KEY_CREATED_AT = "created_at"
MANIFEST_KEY_TARGETS = "targets"
MANIFEST_KEY_SIG = "sig"

MANIFEST_TYPE_MANIFEST = "manifest"
MANIFEST_TYPE_CHUNK = "chunk"
MANIFEST_TYPE_COMPLETE = "complete"

CHUNK_META_KEY_TYPE = "type"
CHUNK_META_KEY_INDEX = "index"
CHUNK_META_KEY_TOTAL = "total"

# =========================
# publisher.env keys (must stay in sync with publisher.env.example).
# =========================
PUB_ENV_KEY_MQTT_BROKER = "MQTT_BROKER"
PUB_ENV_KEY_MQTT_PORT = "MQTT_PORT"
PUB_ENV_KEY_MQTT_USERNAME = "MQTT_USERNAME"
PUB_ENV_KEY_MQTT_PASSWORD = "MQTT_PASSWORD"
PUB_ENV_KEY_MONGO_URI = "MONGO_URI"
PUB_ENV_KEY_MONGO_DB = "MONGO_DB"
PUB_ENV_KEY_MONGO_COLLECTION = "MONGO_COLLECTION"
PUB_ENV_KEY_MONGO_NAME_FIELD = "MONGO_NAME_FIELD"
PUB_ENV_KEY_MONGO_MAC_FIELD = "MONGO_MAC_FIELD"
PUB_ENV_KEY_ACK_WAIT_SECONDS = "ACK_WAIT_SECONDS"

# Mongo field-name defaults (when the env doesn't override them).
MONGO_NAME_FIELD_DEFAULT = "sensorName"
MONGO_MAC_FIELD_DEFAULT = "macAddress"

TARGETS_CONFIG_MODE_ALL = "all"
EXPECTED_TARGET_KEY_NAME = "name"
EXPECTED_TARGET_KEY_MAC = "mac"
REPORTS_DIR_NAME = "reports"

# Operator-identity fallbacks for the audit log.
OPERATOR_ENV_KEY_UNIX = "USER"
OPERATOR_ENV_KEY_WINDOWS = "USERNAME"
OPERATOR_UNKNOWN = "unknown"

# Audit log event names.
AUDIT_EVENT_PASSWORD_SET = "password_set"
AUDIT_EVENT_INVOKED = "invoked"
AUDIT_EVENT_PASSWORD_OK = "password_ok"
AUDIT_EVENT_ABORTED = "aborted"
AUDIT_EVENT_RISK_ACK = "risk_ack"
AUDIT_EVENT_FAILED = "failed"
AUDIT_EVENT_PUBLISHED = "published"

REQUIRED_ENV_KEYS = (
    PUB_ENV_KEY_MQTT_BROKER, PUB_ENV_KEY_MQTT_PORT,
    PUB_ENV_KEY_MQTT_USERNAME, PUB_ENV_KEY_MQTT_PASSWORD,
    PUB_ENV_KEY_MONGO_URI, PUB_ENV_KEY_MONGO_DB, PUB_ENV_KEY_MONGO_COLLECTION,
)

# =========================
# Env + config loaders
# =========================
def _load_publisher_env(env_path: Path) -> dict:
    if not env_path.is_file():
        sys.exit(f"[error] Env file missing: {env_path}")
    values = dotenv_values(env_path)
    missing = [key for key in REQUIRED_ENV_KEYS if not values.get(key)]
    if missing:
        sys.exit(f"[error] Missing keys in {env_path}: {', '.join(missing)}")
    return values


def _load_password_config(path: Path):
    """Return the stored password config dict, or None when not set up yet."""
    if not path.is_file():
        return None
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"[error] {path} is not valid JSON: {e}")
    for required in PASSWORD_CONFIG_REQUIRED_KEYS:
        if required not in config:
            sys.exit(f"[error] {path} missing field: {required}")
    if config[PASSWORD_CONFIG_KEY_KDF] != PASSWORD_KDF:
        sys.exit(f"[error] {path} uses unsupported kdf: {config[PASSWORD_CONFIG_KEY_KDF]}")
    return config


def _derive_password_hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        PASSWORD_HASH_ALGO, password.encode(PASSWORD_ENCODING), salt, iterations,
        dklen=PASSWORD_KEY_BYTES,
    )


def _set_password_interactive(path: Path):
    """First-run flow: prompt operator to set the agent-OTA password."""
    print("[info] No agent-OTA password is set yet.")
    print("[info] Pick a strong password — it gates every publish on this pipeline.")
    while True:
        pw1 = getpass.getpass("New agent-OTA password: ")
        if len(pw1) < PASSWORD_MIN_LENGTH:
            print(f"[warn] Must be at least {PASSWORD_MIN_LENGTH} characters. Try again.")
            continue
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("[warn] Passwords did not match. Try again.")
            continue
        break
    salt = secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = _derive_password_hash(pw1, salt, PASSWORD_ITERATIONS)
    config = {
        PASSWORD_CONFIG_KEY_KDF: PASSWORD_KDF,
        PASSWORD_CONFIG_KEY_ITERATIONS: PASSWORD_ITERATIONS,
        PASSWORD_CONFIG_KEY_SALT_HEX: salt.hex(),
        PASSWORD_CONFIG_KEY_HASH_HEX: digest.hex(),
        PASSWORD_CONFIG_KEY_CREATED_AT: datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(path.suffix + TMP_SUFFIX)
    tmp.write_text(json.dumps(config, indent=2))
    try:
        os.chmod(tmp, PASSWORD_CONFIG_FILE_MODE)
    except OSError:
        # Windows filesystems may not honour this; the file is still gitignored.
        pass
    os.replace(tmp, path)
    print(f"[info] Password saved to {path}. Keep this file private; add it to .gitignore.")
    _audit(AUDIT_EVENT_PASSWORD_SET)
    return config


def _prompt_and_verify_password(config: dict):
    """Ask the operator for the password; exit on mismatch. No retries."""
    entered = getpass.getpass("Agent-OTA publisher password: ")
    salt = bytes.fromhex(config[PASSWORD_CONFIG_KEY_SALT_HEX])
    expected = bytes.fromhex(config[PASSWORD_CONFIG_KEY_HASH_HEX])
    derived = _derive_password_hash(entered, salt, int(config[PASSWORD_CONFIG_KEY_ITERATIONS]))
    if not hmac.compare_digest(derived, expected):
        sys.exit("[error] Password did not match. Aborting.")
    print("[info] Password accepted.")


def _show_risk_prompt_and_require_phrase():
    print(RISK_PROMPT_TEXT)
    response = input(f'Type the exact phrase "{RISK_PROMPT_PHRASE}" to proceed, anything else to abort: ').strip()
    if response != RISK_PROMPT_PHRASE:
        sys.exit("[info] Risk acknowledgement not given. Aborting.")
    print("[info] Risk acknowledged.")


# =========================
# Audit log
# =========================
def _audit(event: str, **details):
    """Append one JSON line to the audit log. Best-effort; never raises."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "operator": (
                os.environ.get(OPERATOR_ENV_KEY_UNIX)
                or os.environ.get(OPERATOR_ENV_KEY_WINDOWS)
                or OPERATOR_UNKNOWN
            ),
            "host": socket.gethostname(),
            "pid": os.getpid(),
            **details,
        }
        with AUDIT_LOG_PATH.open("a", encoding=PASSWORD_ENCODING) as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[warn] Audit log write failed: {e}", file=sys.stderr)


# =========================
# Bundle construction
# =========================
AGENT_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# env-patch section names (must mirror the device's ENV_PATCH_* constants).
PATCH_SECTION_SET = "set"
PATCH_SECTION_SET_IF_MISSING = "set_if_missing"
PATCH_SECTION_REMOVE = "remove"
PATCH_SECTION_TARGETS = "targets"
VALID_PATCH_SECTIONS = frozenset({
    PATCH_SECTION_SET, PATCH_SECTION_SET_IF_MISSING,
    PATCH_SECTION_REMOVE, PATCH_SECTION_TARGETS,
})
PATCH_NOTE_PREFIX = "_"


def _validate_env_patch_file(path: Path):
    """Reject obviously-malformed patches here so the device doesn't have to."""
    try:
        patch = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"[error] {path} is not valid JSON: {e}")
    if not isinstance(patch, dict):
        sys.exit(f"[error] {path} must be a JSON object")
    patch_sections = {key for key in patch if not key.startswith(PATCH_NOTE_PREFIX)}
    unknown = patch_sections - VALID_PATCH_SECTIONS
    if unknown:
        sys.exit(f"[error] {path}: unknown sections {sorted(unknown)}")
    for section in (PATCH_SECTION_SET, PATCH_SECTION_SET_IF_MISSING):
        values = patch.get(section, {})
        if not isinstance(values, dict):
            sys.exit(f"[error] {path}: '{section}' must be a JSON object")
        for key, value in values.items():
            if not AGENT_ENV_KEY_PATTERN.match(str(key)):
                sys.exit(f"[error] {path}: invalid key '{key}' in '{section}'")
            if not isinstance(value, str):
                sys.exit(f"[error] {path}: value of '{key}' in '{section}' must be a string")
    removed = patch.get(PATCH_SECTION_REMOVE, [])
    if not isinstance(removed, list) or not all(isinstance(k, str) and AGENT_ENV_KEY_PATTERN.match(k) for k in removed):
        sys.exit(f"[error] {path}: '{PATCH_SECTION_REMOVE}' must be a list of key names")


def _validate_script_file(path: Path):
    """Pre-check the candidate agent script so a syntactically-broken push never leaves the publisher."""
    import py_compile
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as e:
        sys.exit(f"[error] {path} fails to compile: {e.msg.strip()}")


AGENT_BUNDLE_ARCHIVE_PREFIX = "agent_bundle-"
AGENT_BUNDLE_ARCHIVE_SUFFIX = ".tar.gz"
AGENT_BUNDLE_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
TAR_WRITE_GZIP_MODE = "w:gz"
FILE_READ_BINARY_MODE = "rb"


def build_agent_bundle(script_path, env_path, env_patch_path, out_dir: Path) -> Path:
    """Pack whichever of the three pieces the operator passed into one .tar.gz."""
    timestamp = time.strftime(AGENT_BUNDLE_TIMESTAMP_FORMAT)
    archive_path = out_dir / f"{AGENT_BUNDLE_ARCHIVE_PREFIX}{timestamp}{AGENT_BUNDLE_ARCHIVE_SUFFIX}"
    with tarfile.open(archive_path, TAR_WRITE_GZIP_MODE) as tar:
        if script_path:
            tar.add(script_path, arcname=AGENT_BUNDLE_SCRIPT)
        if env_path:
            tar.add(env_path, arcname=AGENT_BUNDLE_ENV)
        if env_patch_path:
            tar.add(env_patch_path, arcname=AGENT_BUNDLE_ENV_PATCH)
    return archive_path


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open(FILE_READ_BINARY_MODE) as f:
        for chunk in iter(lambda: f.read(SHA256_READ_BUFFER_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


# =========================
# MQTT publish (chunked, same shape as the normal publisher)
# =========================
def send_bundle_over_mqtt(archive_path: Path, broker: str, port: int, username: str, password: str,
                          qos: int, chunk_size: int, manifest_targets: dict,
                          expected_targets: list, ack_wait_seconds: int,
                          reports_dir: Path, group: str) -> dict:
    client = paho.Client(client_id="", userdata=None, protocol=paho.MQTTv5)
    client.tls_set(cert_reqs=paho.ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.username_pw_set(username, password)

    try:
        client.connect(broker, port, keepalive=MQTT_KEEPALIVE_SECS)
        client.loop_start()

        data = archive_path.read_bytes()
        total_size = len(data)
        total_chunks = (total_size + chunk_size - 1) // chunk_size
        file_hash = hashlib.sha256(data).hexdigest()

        session_id = str(uuid.uuid4())
        topic_prefix = f"{AGENT_OTA_TOPIC}/{session_id}"
        manifest_topic = f"{topic_prefix}/{MANIFEST_TYPE_MANIFEST}"
        complete_topic = f"{topic_prefix}/{MANIFEST_TYPE_COMPLETE}"

        collector = DeploymentAckCollector(
            group=group,
            session_id=session_id,
            expected_targets=expected_targets,
            reports_dir=reports_dir,
            wait_seconds=ack_wait_seconds,
        )
        collector.attach(client)

        manifest = {
            MANIFEST_KEY_TYPE: MANIFEST_TYPE_MANIFEST,
            MANIFEST_KEY_SESSION_ID: session_id,
            MANIFEST_KEY_FILENAME: archive_path.name,
            MANIFEST_KEY_SIZE_BYTES: total_size,
            MANIFEST_KEY_SHA256: file_hash,
            MANIFEST_KEY_CHUNK_SIZE: chunk_size,
            MANIFEST_KEY_TOTAL_CHUNKS: total_chunks,
            MANIFEST_KEY_CREATED_AT: int(time.time()),
            MANIFEST_KEY_TARGETS: manifest_targets or {"mode": TARGETS_CONFIG_MODE_ALL},
            # Forward-compatible slot; the device agent does not verify it yet.
            MANIFEST_KEY_SIG: None,
        }

        info = client.publish(manifest_topic, json.dumps(manifest), qos=qos, retain=RETAIN_MANIFEST)
        info.wait_for_publish()

        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, total_size)
            payload = data[start:end]
            meta = {
                CHUNK_META_KEY_TYPE: MANIFEST_TYPE_CHUNK,
                CHUNK_META_KEY_INDEX: i,
                CHUNK_META_KEY_TOTAL: total_chunks,
            }
            client.publish(f"{topic_prefix}/chunks/meta/{i}", json.dumps(meta), qos=qos)
            info = client.publish(f"{topic_prefix}/chunks/data/{i}", payload, qos=qos)
            info.wait_for_publish()

        complete = {
            MANIFEST_KEY_TYPE: MANIFEST_TYPE_COMPLETE,
            MANIFEST_KEY_SESSION_ID: session_id,
            MANIFEST_KEY_SHA256: file_hash,
        }
        info = client.publish(complete_topic, json.dumps(complete), qos=qos)
        info.wait_for_publish()

        print(f"[info] Waiting up to {ack_wait_seconds}s for device acks ...")
        ack_summary = collector.wait()
        report_path = collector.write_xlsx()
        print(f"[info] Ack report: {report_path}  ({ack_summary})")

        return {
            "session_id": session_id,
            "topic_prefix": topic_prefix,
            "sha256": file_hash,
            "total_chunks": total_chunks,
            "size_bytes": total_size,
            "ack_summary": ack_summary,
            "ack_report": str(report_path),
        }
    finally:
        client.loop_stop()
        client.disconnect()


# =========================
# Main flow
# =========================
def _parse_args():
    parser = argparse.ArgumentParser(
        description="Publish an agent self-update over the OTA_AGENT MQTT topic.",
    )
    parser.add_argument("--script", type=Path, help="Path to the new ota_agent.py to ship.")
    parser.add_argument("--agent-env", type=Path, help="Path to a full replacement agent.env.")
    parser.add_argument("--agent-env-patch", type=Path, help="Path to an agent_env_patch.json diff.")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE_BYTES)
    parser.add_argument("--qos", type=int, default=DEFAULT_QOS, choices=(0, 1, 2))
    args = parser.parse_args()
    if args.agent_env and args.agent_env_patch:
        parser.error("--agent-env and --agent-env-patch are mutually exclusive (the device rejects a bundle with both).")
    if not (args.script or args.agent_env or args.agent_env_patch):
        parser.error("Nothing to publish. Pass at least one of --script / --agent-env / --agent-env-patch.")
    return args


def _resolve_inputs(args):
    """Resolve + validate every input file before asking for the password."""
    script = args.script.resolve() if args.script else None
    env = args.agent_env.resolve() if args.agent_env else None
    patch = args.agent_env_patch.resolve() if args.agent_env_patch else None

    if script:
        if not script.is_file():
            sys.exit(f"[error] --script file not found: {script}")
        _validate_script_file(script)
    if env:
        if not env.is_file():
            sys.exit(f"[error] --agent-env file not found: {env}")
    if patch:
        if not patch.is_file():
            sys.exit(f"[error] --agent-env-patch file not found: {patch}")
        _validate_env_patch_file(patch)
    return script, env, patch


def main():
    args = _parse_args()
    script, env, patch = _resolve_inputs(args)
    _audit(AUDIT_EVENT_INVOKED, script=str(script) if script else None,
                     agent_env=str(env) if env else None,
                     agent_env_patch=str(patch) if patch else None)

    env_values = _load_publisher_env(PUBLISHER_ENV_PATH)

    # Password gate ---------------------------------------------------------
    # First-run writes the password hash but is NOT treated as authentication:
    # the operator must re-enter the password they just set to get past the
    # gate, so anyone who happens to run the tool before a real operator cannot
    # self-authorise a publish by creating the config themselves.
    password_config = _load_password_config(AGENT_PASSWORD_CONFIG_PATH)
    if password_config is None:
        password_config = _set_password_interactive(AGENT_PASSWORD_CONFIG_PATH)
        print("[info] Re-enter the password you just set to continue.")
    _prompt_and_verify_password(password_config)
    _audit(AUDIT_EVENT_PASSWORD_OK)

    # Target resolution ----------------------------------------------------
    try:
        targets_config = load_targets_config(AGENT_TARGETS_CONFIG_PATH)
        all_devices = fetch_devices(
            env_values[PUB_ENV_KEY_MONGO_URI],
            env_values[PUB_ENV_KEY_MONGO_DB],
            env_values[PUB_ENV_KEY_MONGO_COLLECTION],
            env_values.get(PUB_ENV_KEY_MONGO_NAME_FIELD) or MONGO_NAME_FIELD_DEFAULT,
            env_values.get(PUB_ENV_KEY_MONGO_MAC_FIELD) or MONGO_MAC_FIELD_DEFAULT,
        )
        if not all_devices:
            sys.exit(f"[error] MongoDB collection '{env_values[PUB_ENV_KEY_MONGO_COLLECTION]}' has no devices")
        resolved = resolve_targets(targets_config, all_devices)
    except TargetingError as e:
        _audit(AUDIT_EVENT_ABORTED, reason=f"targeting error: {e}")
        sys.exit(f"[error] {e}")

    manifest_targets = build_manifest_targets(resolved)
    effective_macs = resolved["effective_macs"]
    if not effective_macs:
        _audit(AUDIT_EVENT_ABORTED, reason="empty target set")
        sys.exit("[error] Target set is empty, nothing to deploy to.")

    # Risk acknowledgement -------------------------------------------------
    print(f"\n[info] Target set ({len(effective_macs)} device(s)):")
    for mac in effective_macs:
        print(f"  - {mac}")
    print("\n[info] Bundle contents:")
    print(f"  - script: {script if script else '(not included)'}")
    print(f"  - agent.env: {env if env else '(not included)'}")
    print(f"  - agent_env_patch.json: {patch if patch else '(not included)'}")

    _show_risk_prompt_and_require_phrase()
    _audit(AUDIT_EVENT_RISK_ACK)

    # Build bundle ---------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="agent_bundle_") as tmp:
        out_dir = Path(tmp)
        archive_path = build_agent_bundle(script, env, patch, out_dir)
        file_hash = sha256_of_file(archive_path)
        print(f"[info] Archive built: {archive_path.name} ({archive_path.stat().st_size} bytes)")
        print(f"[info] SHA256: {file_hash}")

        if resolved["config_mode"] != TARGETS_CONFIG_MODE_ALL:
            expected_targets = resolved["requested_devices"]
        else:
            expected_targets = [
                {EXPECTED_TARGET_KEY_NAME: m, EXPECTED_TARGET_KEY_MAC: m}
                for m in effective_macs
            ]
        ack_wait_seconds = int(env_values.get(PUB_ENV_KEY_ACK_WAIT_SECONDS) or DEFAULT_ACK_WAIT_SECONDS)

        print(f"[info] Publishing to {env_values[PUB_ENV_KEY_MQTT_BROKER]}:{env_values[PUB_ENV_KEY_MQTT_PORT]} topic '{AGENT_OTA_TOPIC}' ...")
        try:
            result = send_bundle_over_mqtt(
                archive_path=archive_path,
                broker=env_values[PUB_ENV_KEY_MQTT_BROKER],
                port=int(env_values[PUB_ENV_KEY_MQTT_PORT]),
                username=env_values[PUB_ENV_KEY_MQTT_USERNAME],
                password=env_values[PUB_ENV_KEY_MQTT_PASSWORD],
                qos=args.qos,
                chunk_size=args.chunk_size,
                manifest_targets=manifest_targets,
                expected_targets=expected_targets,
                ack_wait_seconds=ack_wait_seconds,
                reports_dir=SERVER_DIR / REPORTS_DIR_NAME,
                group=PUBLISH_GROUP_AGENT,
            )
        except Exception as e:
            _audit(AUDIT_EVENT_FAILED, reason=f"{type(e).__name__}: {e}")
            sys.exit(f"[error] MQTT publish failed: {e}")

        write_resolved_file(AGENT_TARGETS_RESOLVED_PATH, PUBLISH_GROUP_AGENT, resolved, manifest_targets, archive_path.name)
        _audit(
            AUDIT_EVENT_PUBLISHED,
            session_id=result["session_id"],
            sha256=file_hash,
            target_count=len(effective_macs),
            bundle=archive_path.name,
            ack_summary=result.get("ack_summary"),
            ack_report=result.get("ack_report"),
        )
        print(f"[info] Target audit written to {AGENT_TARGETS_RESOLVED_PATH}")
        print("[info] Publish complete.")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
