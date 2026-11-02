#!/usr/bin/env python3
import os
import sys
import tarfile
import tempfile
import time
import json
import hashlib
import uuid
from fnmatch import fnmatch
from pathlib import Path

import paho.mqtt.client as paho
from dotenv import dotenv_values

from app_env_source import APP_ENV_ARCHIVE_NAME, APP_ENV_SOURCE_PATH, validate_app_env_source

# =========================
# Env loader
# =========================
PUBLISHER_ENV_PATH = Path(__file__).resolve().parent / "publisher.env"

REQUIRED_ENV_KEYS = (
    "REPO_PATHS", "DEVICE_GROUP",
    "MQTT_BROKER", "MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_BASE_TOPIC",
)


def _load_publisher_env(env_path: Path) -> dict:
    if not env_path.is_file():
        print(f"[error] Env file missing: {env_path}", file=sys.stderr)
        sys.exit(1)
    values = dotenv_values(env_path)
    missing = [key for key in REQUIRED_ENV_KEYS if not values.get(key)]
    if missing:
        print(f"[error] Missing keys in {env_path}: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    return values


def _env_bool(values: dict, key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_ENV = _load_publisher_env(PUBLISHER_ENV_PATH)

# =========================
# Configuration
# =========================
REPO_PATHS = [p.strip() for p in _ENV["REPO_PATHS"].split(",") if p.strip()]  # local repo path(s)

# What to include in the tar. By default, whole repo (excluding .git).
# You can restrict to specific subfolders or file patterns if you like.
INCLUDE_PATHS = []  # e.g., ["src", "scripts", "requirements.txt"]

# Never shipped from the repo folder: device-owned or local-only files. Each
# device keeps its own app.env, logs and database; this stops the laptop's
# copies from leaving the laptop. The bundle's app.env comes from
# APP_ENV_SOURCE_PATH instead (see app_env_source.py).
# Do not update THERMAL_SENSOR through OTA: each device's sensor is set at
# device setup, and the bundle's app.env must not contain it.
EXCLUDED_DIR_NAMES = frozenset({".git", "logs", "__pycache__"})
EXCLUDED_FILE_PATTERNS = (
    ".env",
    "app.env",
    "*.db", "*.db-wal", "*.db-shm", "*.db-journal",
    "*.pkl",
    "*.pyc",
)

ARCHIVE_BASENAME = _ENV.get("ARCHIVE_BASENAME") or "repo_bundle"

MQTT_BROKER = _ENV["MQTT_BROKER"]
MQTT_PORT = int(_ENV["MQTT_PORT"])
MQTT_USERNAME = _ENV["MQTT_USERNAME"]
MQTT_PASSWORD = _ENV["MQTT_PASSWORD"]
MQTT_BASE_TOPIC = _ENV["MQTT_BASE_TOPIC"]

# Chunking (many brokers default-limit payloads; 256–512 KB is usually safe)
CHUNK_SIZE = int(_ENV.get("CHUNK_SIZE") or 256 * 1024)  # 256 KB
QOS = int(_ENV.get("QOS") or 1)                  # 0, 1 or 2; use 1 for at-least-once
RETAIN_MANIFEST = _env_bool(_ENV, "RETAIN_MANIFEST", False)  # typically False for updates

# =========================
# Helpers
# =========================
def make_tar_gz(source_dir: Path, include_paths, out_dir: Path, base_name: str, app_env_source: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    archive_name = f"{base_name}-{timestamp}.tar.gz"
    archive_path = out_dir / archive_name

    def should_include(p: Path) -> bool:
        # Exclude device-owned/local-only files and the output archive itself.
        # Check only the path inside the repo, so a parent folder named e.g.
        # "logs" on the laptop doesn't exclude everything.
        relative_parts = p.relative_to(source_dir).parts
        if any(part in EXCLUDED_DIR_NAMES for part in relative_parts[:-1]):
            return False
        if any(fnmatch(p.name, pattern) for pattern in EXCLUDED_FILE_PATTERNS):
            return False
        if archive_path.exists() and p.exists() and archive_path.samefile(p):
            return False
        return True

    with tarfile.open(archive_path, "w:gz") as tar:
        if include_paths:
            for inc in include_paths:
                src = (source_dir / inc).resolve()
                if not src.exists():
                    print(f"[warn] include path missing: {src}", file=sys.stderr)
                    continue
                if src.is_dir():
                    for root, dirs, files in os.walk(src):
                        root_path = Path(root)
                        # Skip excluded dirs (.git, logs, __pycache__)
                        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
                        for f in files:
                            fp = root_path / f
                            if should_include(fp):
                                tar.add(fp, arcname=str(fp.relative_to(source_dir)))
                else:
                    if should_include(src):
                        tar.add(src, arcname=str(src.relative_to(source_dir)))
        else:
            # Add the entire repo, minus the excluded dirs and files
            for root, dirs, files in os.walk(source_dir):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
                for f in files:
                    fp = root_path / f
                    if should_include(fp):
                        tar.add(fp, arcname=str(fp.relative_to(source_dir)))

        tar.add(app_env_source, arcname=APP_ENV_ARCHIVE_NAME)

    return archive_path

def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1_048_576), b""):  # 1 MB read chunks
            h.update(chunk)
    return h.hexdigest()

# =========================
# MQTT send utility
# =========================
def send_archive_over_mqtt(archive_path: Path, base_topic: str, broker: str, port: int,
                           username=None, password=None, qos=1, retain_manifest=False, chunk_size=256*1024):
    try:
        client = paho.Client(client_id="", userdata=None, protocol=paho.MQTTv5)
        client.tls_set(cert_reqs=paho.ssl.CERT_NONE)  # disable CA check (since you're using self-signed)
        client.tls_insecure_set(True)
        if username and password:
            client.username_pw_set(username, password)

        # Synchronous connect/publish is fine for a one-shot script
        client.connect(broker, port, keepalive=60)

        client.loop_start()

        with archive_path.open("rb") as f:
            data = f.read()

        total_size = len(data)
        total_chunks = (total_size + chunk_size - 1) // chunk_size
        file_hash = hashlib.sha256(data).hexdigest()

        session_id = str(uuid.uuid4())
        topic_prefix = f"{base_topic}/{session_id}"

        manifest = {
            "type": "manifest",
            "session_id": session_id,
            "filename": archive_path.name,
            "size_bytes": total_size,
            "sha256": file_hash,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "created_at": int(time.time())
        }

        # Publish manifest first
        info = client.publish(f"{topic_prefix}/manifest", json.dumps(manifest), qos=qos, retain=retain_manifest)
        info.wait_for_publish()

        # Publish each chunk
        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, total_size)
            payload = data[start:end]
            # Add a tiny header so the receiver can verify order/size easily
            chunk_msg = {
                "type": "chunk",
                "index": i,
                "total": total_chunks,
            }
            # We'll publish a small JSON header to /chunks/meta and raw bytes to /chunks/data
            client.publish(f"{topic_prefix}/chunks/meta/{i}", json.dumps(chunk_msg), qos=qos)
            info = client.publish(f"{topic_prefix}/chunks/data/{i}", payload, qos=qos)
            info.wait_for_publish()

        # Publish a completion message
        complete = {"type": "complete", "session_id": session_id, "sha256": file_hash}
        info = client.publish(f"{topic_prefix}/complete", json.dumps(complete), qos=qos)
        info.wait_for_publish()

        client.loop_stop()
        client.disconnect()

        return {
            "session_id": session_id,
            "topic_prefix": topic_prefix,
            "sha256": file_hash,
            "total_chunks": total_chunks,
            "size_bytes": total_size,
        }
    except Exception as e:
        print(f"[error] MQTT send failed: {e}", file=sys.stderr)

# =========================
# Main flow
# =========================
def main():
    for i in range(len(REPO_PATHS)):
        REPO_PATH = REPO_PATHS[i]
        repo = Path(REPO_PATH).resolve()
        if not repo.exists():
            print(f"[error] REPO_PATH does not exist: {repo}", file=sys.stderr)
            sys.exit(1)

        try:
            DEVICE_GROUP = _ENV["DEVICE_GROUP"]

            app_env_source = validate_app_env_source(APP_ENV_SOURCE_PATH)

            out_dir = Path(tempfile.mkdtemp(prefix="repo_bundle_"))
            archive_path = make_tar_gz(repo, INCLUDE_PATHS, out_dir, f"{ARCHIVE_BASENAME}-{DEVICE_GROUP}", app_env_source)
            print(f"[info] Archive created: {archive_path} ({archive_path.stat().st_size} bytes)")

            file_hash = sha256_of_file(archive_path)
            print(f"[info] SHA256: {file_hash}")

            print(f"[info] Sending over MQTT to {MQTT_BROKER}:{MQTT_PORT} topic '{MQTT_BASE_TOPIC}/{DEVICE_GROUP}' ...")
            result = send_archive_over_mqtt(
                archive_path=archive_path,
                base_topic=MQTT_BASE_TOPIC + f"/{DEVICE_GROUP}",
                broker=MQTT_BROKER,
                port=MQTT_PORT,
                username=MQTT_USERNAME,
                password=MQTT_PASSWORD,
                qos=QOS,
                retain_manifest=RETAIN_MANIFEST,
                chunk_size=CHUNK_SIZE,
            )

            print("[info] MQTT send complete.")
            print(json.dumps(result, indent=2))

        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
            sys.exit(2)

if __name__ == "__main__":
    main()
