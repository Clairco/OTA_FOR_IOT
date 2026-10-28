#!/usr/bin/env python3
import os
import sys
import subprocess
import tarfile
import tempfile
import time
import json
import hashlib
import uuid
from fnmatch import fnmatch
from pathlib import Path

import paho.mqtt.client as paho

# =========================
# Configuration
# =========================
# "C:\Users\HP\Downloads\AZ Chennai 11June\AZ Chennai\Desk\prod"
REPO_PATHS = ['C:/Users/HP/Downloads/AZ Chennai 11June/AZ Chennai/Desk/prod']          # local git repo path
REMOTE = "origin"                           # typically 'origin'
BRANCHES = ['thermophileTesting']                             # branch to track (e.g., main/master/dev)

# What to include in the tar. By default, whole repo (excluding .git).
# You can restrict to specific subfolders or file patterns if you like.
INCLUDE_PATHS = []  # e.g., ["src", "scripts", "requirements.txt"]

# Never shipped: device-owned or local-only files. Each device keeps its own
# .env, logs and database; the OTA agent also refuses to take these from a
# bundle, this just stops the laptop's copies from leaving the laptop.
EXCLUDED_DIR_NAMES = frozenset({".git", "logs", "__pycache__"})
EXCLUDED_FILE_PATTERNS = (
    ".env",
    "*.db", "*.db-wal", "*.db-shm", "*.db-journal",
    "*.pkl",
    "*.pyc",
)

ARCHIVE_BASENAME = "repo_bundle"           # archive base name (timestamp & commit appended)
OUTPUT_DIR = "C:/Users/HP/Desktop/github/"  # or any writable path                         # None = temp dir; or set a path to keep archives
 # base topic — we'll publish manifest + chunks under this
MQTT_BROKER="172.28.26.11"
MQTT_PORT=8883
MQTT_USERNAME='AZCHE_MQTT'
MQTT_PASSWORD='AstraZeneca@4Ct'
MQTT_BASE_TOPIC="ota/updates"

# Chunking (many brokers default-limit payloads; 256–512 KB is usually safe)
CHUNK_SIZE = 256 * 1024  # 256 KB
QOS = 1                  # 0, 1 or 2; use 1 for at-least-once
RETAIN_MANIFEST = False  # typically False for updates

# =========================
# Helpers
# =========================
def run(cmd, cwd=None) -> str:
    """Run a shell command and return stdout (str). Raises on non-zero exit."""
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDERR:\n{proc.stderr}")
    return proc.stdout.strip()

def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()

def get_local_head(path: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=str(path))

def get_remote_head(path: Path, remote: str, branch: str) -> str:
    return run(["git", "rev-parse", f"{remote}/{branch}"], cwd=str(path))

def fetch_remote(path: Path):
    run(["git", "fetch", "--all", "--prune"], cwd=str(path))

def pull_changes(path: Path, remote: str, branch: str):
    run(["git", "pull", remote, branch], cwd=str(path))

def make_tar_gz(source_dir: Path, include_paths, out_dir: Path, base_name: str) -> Path:
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
        # if not is_git_repo(repo):
        #     print(f"[error] Not a git repository: {repo}", file=sys.stderr)
        #     sys.exit(1)

        try:
            print("[info] Fetching remote...")
            # fetch_remote(repo)
            # local_head = get_local_head(repo)
            # thermophile thermopile THERMOPILECHE2

            BRANCH = 'thermopile'
            # remote_head = get_remote_head(repo, REMOTE, BRANCH)
            # print(f"[info] local HEAD : {local_head}")
            # print(f"[info] remote HEAD: {remote_head}")

            changed = 1 #(local_head != remote_head)
            if changed:
                print("[info] Changes detected. Pulling...")
                # pull_changes(repo, REMOTE, BRANCH)
                # recompute local head after pull
                # local_head = get_local_head(repo)
                # print(f"[info] Updated local HEAD: {local_head}")

                # Always build (you can conditionally build only if changed)
                out_dir =  Path(tempfile.mkdtemp(prefix="repo_bundle_"))
                archive_path = make_tar_gz(repo, INCLUDE_PATHS, out_dir, f"{ARCHIVE_BASENAME}-{BRANCH}") # f"{ARCHIVE_BASENAME-{local_head[:7]}")
                print(f"[info] Archive created: {archive_path} ({archive_path.stat().st_size} bytes)")

                file_hash = sha256_of_file(archive_path)
                print(f"[info] SHA256: {file_hash}")

                print(f"[info] Sending over MQTT to {MQTT_BROKER}:{MQTT_PORT} topic '{MQTT_BASE_TOPIC}{f'/{BRANCH}'}' ...")
                result = send_archive_over_mqtt(
                    archive_path=archive_path,
                    base_topic=MQTT_BASE_TOPIC+f"/{BRANCH}",
                    broker=MQTT_BROKER,
                    port=MQTT_PORT,
                    username=MQTT_USERNAME,
                    password=MQTT_PASSWORD,
                    qos=QOS,
                    retain_manifest=RETAIN_MANIFEST,
                    chunk_size=CHUNK_SIZE
                )

                print("[info] MQTT send complete.")
                print(json.dumps(result, indent=2))
            else:
                print("[info] No changes. Using current working tree.")


        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
            sys.exit(2)

if __name__ == "__main__":
    main()


# rm -rf ~/.pm2/logs/*