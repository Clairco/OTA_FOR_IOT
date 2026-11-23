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

REQUIRED_ENV_KEYS = (
    "MQTT_BROKER", "MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD",
    "MONGO_URI", "MONGO_DB", "MONGO_COLLECTION",
)

# =========================
# Password gate (PBKDF2, stdlib only)
# =========================
# We store only a salted PBKDF2 hash on disk. Someone who reads the config
# file must brute-force the password to use it. Rotating the password just
# means deleting the file and running the tool once to re-prompt.
PASSWORD_KDF = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 200_000
PASSWORD_SALT_BYTES = 16
PASSWORD_KEY_BYTES = 32

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
DEFAULT_CHUNK_SIZE = 256 * 1024
DEFAULT_QOS = 1
RETAIN_MANIFEST = False

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
    for required in ("kdf", "iterations", "salt_hex", "hash_hex"):
        if required not in config:
            sys.exit(f"[error] {path} missing field: {required}")
    if config["kdf"] != PASSWORD_KDF:
        sys.exit(f"[error] {path} uses unsupported kdf: {config['kdf']}")
    return config


def _derive_password_hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=PASSWORD_KEY_BYTES)


def _set_password_interactive(path: Path):
    """First-run flow: prompt operator to set the agent-OTA password."""
    print("[info] No agent-OTA password is set yet.")
    print("[info] Pick a strong password — it gates every publish on this pipeline.")
    while True:
        pw1 = getpass.getpass("New agent-OTA password: ")
        if len(pw1) < 8:
            print("[warn] Must be at least 8 characters. Try again.")
            continue
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("[warn] Passwords did not match. Try again.")
            continue
        break
    salt = secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = _derive_password_hash(pw1, salt, PASSWORD_ITERATIONS)
    config = {
        "kdf": PASSWORD_KDF,
        "iterations": PASSWORD_ITERATIONS,
        "salt_hex": salt.hex(),
        "hash_hex": digest.hex(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Windows filesystems may not honour this; the file is still gitignored.
        pass
    os.replace(tmp, path)
    print(f"[info] Password saved to {path}. Keep this file private; add it to .gitignore.")
    return config


def _prompt_and_verify_password(config: dict):
    """Ask the operator for the password; exit on mismatch. No retries."""
    entered = getpass.getpass("Agent-OTA publisher password: ")
    salt = bytes.fromhex(config["salt_hex"])
    expected = bytes.fromhex(config["hash_hex"])
    derived = _derive_password_hash(entered, salt, int(config["iterations"]))
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
            "operator": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
            "host": socket.gethostname(),
            "pid": os.getpid(),
            **details,
        }
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[warn] Audit log write failed: {e}", file=sys.stderr)


# =========================
# Bundle construction
# =========================
AGENT_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VALID_PATCH_SECTIONS = frozenset({"set", "set_if_missing", "remove", "targets"})


def _validate_env_patch_file(path: Path):
    """Reject obviously-malformed patches here so the device doesn't have to."""
    try:
        patch = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"[error] {path} is not valid JSON: {e}")
    if not isinstance(patch, dict):
        sys.exit(f"[error] {path} must be a JSON object")
    patch_sections = {key for key in patch if not key.startswith("_")}
    unknown = patch_sections - VALID_PATCH_SECTIONS
    if unknown:
        sys.exit(f"[error] {path}: unknown sections {sorted(unknown)}")
    for section in ("set", "set_if_missing"):
        values = patch.get(section, {})
        if not isinstance(values, dict):
            sys.exit(f"[error] {path}: '{section}' must be a JSON object")
        for key, value in values.items():
            if not AGENT_ENV_KEY_PATTERN.match(str(key)):
                sys.exit(f"[error] {path}: invalid key '{key}' in '{section}'")
            if not isinstance(value, str):
                sys.exit(f"[error] {path}: value of '{key}' in '{section}' must be a string")
    removed = patch.get("remove", [])
    if not isinstance(removed, list) or not all(isinstance(k, str) and AGENT_ENV_KEY_PATTERN.match(k) for k in removed):
        sys.exit(f"[error] {path}: 'remove' must be a list of key names")


def _validate_script_file(path: Path):
    """Pre-check the candidate agent script so a syntactically-broken push never leaves the publisher."""
    import py_compile
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as e:
        sys.exit(f"[error] {path} fails to compile: {e.msg.strip()}")


def build_agent_bundle(script_path, env_path, env_patch_path, out_dir: Path) -> Path:
    """Pack whichever of the three pieces the operator passed into one .tar.gz."""
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    archive_path = out_dir / f"agent_bundle-{timestamp}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        if script_path:
            tar.add(script_path, arcname=AGENT_BUNDLE_SCRIPT)
        if env_path:
            tar.add(env_path, arcname=AGENT_BUNDLE_ENV)
        if env_patch_path:
            tar.add(env_patch_path, arcname=AGENT_BUNDLE_ENV_PATCH)
    return archive_path


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1_048_576), b""):
            h.update(chunk)
    return h.hexdigest()


# =========================
# MQTT publish (chunked, same shape as the normal publisher)
# =========================
def send_bundle_over_mqtt(archive_path: Path, broker: str, port: int, username: str, password: str,
                          qos: int, chunk_size: int, manifest_targets: dict) -> dict:
    client = paho.Client(client_id="", userdata=None, protocol=paho.MQTTv5)
    client.tls_set(cert_reqs=paho.ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.username_pw_set(username, password)

    try:
        client.connect(broker, port, keepalive=60)
        client.loop_start()

        data = archive_path.read_bytes()
        total_size = len(data)
        total_chunks = (total_size + chunk_size - 1) // chunk_size
        file_hash = hashlib.sha256(data).hexdigest()

        session_id = str(uuid.uuid4())
        topic_prefix = f"{AGENT_OTA_TOPIC}/{session_id}"

        manifest = {
            "type": "manifest",
            "session_id": session_id,
            "filename": archive_path.name,
            "size_bytes": total_size,
            "sha256": file_hash,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "created_at": int(time.time()),
            "targets": manifest_targets or {"mode": "all"},
            # Forward-compatible slot; the device agent does not verify it yet.
            "sig": None,
        }

        info = client.publish(f"{topic_prefix}/manifest", json.dumps(manifest), qos=qos, retain=RETAIN_MANIFEST)
        info.wait_for_publish()

        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, total_size)
            payload = data[start:end]
            meta = {"type": "chunk", "index": i, "total": total_chunks}
            client.publish(f"{topic_prefix}/chunks/meta/{i}", json.dumps(meta), qos=qos)
            info = client.publish(f"{topic_prefix}/chunks/data/{i}", payload, qos=qos)
            info.wait_for_publish()

        complete = {"type": "complete", "session_id": session_id, "sha256": file_hash}
        info = client.publish(f"{topic_prefix}/complete", json.dumps(complete), qos=qos)
        info.wait_for_publish()

        return {
            "session_id": session_id,
            "topic_prefix": topic_prefix,
            "sha256": file_hash,
            "total_chunks": total_chunks,
            "size_bytes": total_size,
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
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
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
    _audit("invoked", script=str(script) if script else None,
                     agent_env=str(env) if env else None,
                     agent_env_patch=str(patch) if patch else None)

    env_values = _load_publisher_env(PUBLISHER_ENV_PATH)

    # Password gate ---------------------------------------------------------
    password_config = _load_password_config(AGENT_PASSWORD_CONFIG_PATH)
    if password_config is None:
        password_config = _set_password_interactive(AGENT_PASSWORD_CONFIG_PATH)
        # On first-run the operator just set it, so we treat that as the auth.
    else:
        _prompt_and_verify_password(password_config)
    _audit("password_ok")

    # Target resolution ----------------------------------------------------
    try:
        targets_config = load_targets_config(AGENT_TARGETS_CONFIG_PATH)
        all_devices = fetch_devices(
            env_values["MONGO_URI"],
            env_values["MONGO_DB"],
            env_values["MONGO_COLLECTION"],
            env_values.get("MONGO_NAME_FIELD") or "sensorName",
            env_values.get("MONGO_MAC_FIELD") or "macAddress",
        )
        if not all_devices:
            sys.exit(f"[error] MongoDB collection '{env_values['MONGO_COLLECTION']}' has no devices")
        resolved = resolve_targets(targets_config, all_devices)
    except TargetingError as e:
        _audit("aborted", reason=f"targeting error: {e}")
        sys.exit(f"[error] {e}")

    manifest_targets = build_manifest_targets(resolved)
    effective_macs = resolved["effective_macs"]
    if not effective_macs:
        _audit("aborted", reason="empty target set")
        sys.exit("[error] Target set is empty, nothing to deploy to.")

    # Risk acknowledgement -------------------------------------------------
    print(f"\n[info] Target set ({len(effective_macs)} device(s)):")
    for mac in effective_macs:
        print(f"  - {mac}")
    print(f"\n[info] Bundle contents:")
    print(f"  - script: {script if script else '(not included)'}")
    print(f"  - agent.env: {env if env else '(not included)'}")
    print(f"  - agent_env_patch.json: {patch if patch else '(not included)'}")

    _show_risk_prompt_and_require_phrase()
    _audit("risk_ack")

    # Build bundle ---------------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="agent_bundle_") as tmp:
        out_dir = Path(tmp)
        archive_path = build_agent_bundle(script, env, patch, out_dir)
        file_hash = sha256_of_file(archive_path)
        print(f"[info] Archive built: {archive_path.name} ({archive_path.stat().st_size} bytes)")
        print(f"[info] SHA256: {file_hash}")

        print(f"[info] Publishing to {env_values['MQTT_BROKER']}:{env_values['MQTT_PORT']} topic '{AGENT_OTA_TOPIC}' ...")
        try:
            result = send_bundle_over_mqtt(
                archive_path=archive_path,
                broker=env_values["MQTT_BROKER"],
                port=int(env_values["MQTT_PORT"]),
                username=env_values["MQTT_USERNAME"],
                password=env_values["MQTT_PASSWORD"],
                qos=args.qos,
                chunk_size=args.chunk_size,
                manifest_targets=manifest_targets,
            )
        except Exception as e:
            _audit("failed", reason=f"{type(e).__name__}: {e}")
            sys.exit(f"[error] MQTT publish failed: {e}")

        write_resolved_file(AGENT_TARGETS_RESOLVED_PATH, "ota_agent", resolved, manifest_targets, archive_path.name)
        _audit(
            "published",
            session_id=result["session_id"],
            sha256=file_hash,
            target_count=len(effective_macs),
            bundle=archive_path.name,
        )
        print(f"[info] Target audit written to {AGENT_TARGETS_RESOLVED_PATH}")
        print("[info] Publish complete.")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
