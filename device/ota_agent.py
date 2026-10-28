import os
import shutil
import tarfile
import json
import hashlib
import platform
import subprocess
import traceback
import sqlite3
from pathlib import Path
import paho.mqtt.client as paho
from dotenv import load_dotenv
load_dotenv()

# ==============================
# Configuration
# ==============================

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


BROKER = get_broker_from_db()
PORT = os.getenv("MQTT_PORT")
USER = os.getenv("MQTT_USERNAME")
PASSWORD = os.getenv("MQTT_PASSWORD")
TOPIC = os.getenv("MQTT_BASE_TOPIC")

STAGING_DIR = Path(os.getenv("STAGING_DIR"))          # temp extract location
LIVE_CODE_DIR = Path(os.getenv("PROD_DIR"))               # your running code location

# Device-owned entries at the top level of PROD_DIR. An update never deletes
# them and never takes them from the bundle, so each device keeps its own
# configuration and data.
PRESERVED_PATHS = frozenset({
    ".env",         # device config: THERMAL_SENSOR, broker, LOG_DIR, ...
    "logs",         # device logs collected for diagnosis
    "iot_data.db",  # local zone/config database, when it lives in PROD_DIR
})

# The device .env must exist before an update is applied. The bundle's
# sample.env lists every key the new code reads; keys missing from the
# device .env are reported in the ack.
DEVICE_ENV_FILE = ".env"
SAMPLE_ENV_FILE = "sample.env"
ENV_COMMENT_PREFIX = "#"
ENV_EXPORT_PREFIX = "export "
ENV_KEY_SEPARATOR = "="

ACK_TOPIC = "OTA/ack"
ACK_QOS = 2
ACK_STATUS_UPDATED = "updated"
ACK_STATUS_FAILED = "failed"

#print(BROKER,PORT , USER , PASSWORD ,TOPIC)

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
        shutil.move(str(item), str(live / item.name))

# ==============================
# Device .env checks
# ==============================
def read_env_keys(env_path: Path) -> set:
    """Return the keys defined in a .env-style file (comments and blank lines ignored)."""
    keys = set()
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(ENV_COMMENT_PREFIX) or ENV_KEY_SEPARATOR not in line:
            continue
        if line.startswith(ENV_EXPORT_PREFIX):
            line = line[len(ENV_EXPORT_PREFIX):]
        keys.add(line.split(ENV_KEY_SEPARATOR, 1)[0].strip())
    return keys


def check_device_env(staging: Path, live: Path) -> list:
    """
    Check the device .env before the live code is replaced.

    Raises RuntimeError when the device has no .env: the bundle's .env is never
    used, so deploying would start the code on defaults (wrong sensor, wrong
    broker). Returns the keys in the bundle's sample.env that the device .env
    does not define (empty when the bundle has no sample.env).
    """
    device_env = live / DEVICE_ENV_FILE
    if not device_env.is_file():
        raise RuntimeError(f"device {DEVICE_ENV_FILE} missing in {live}, update not applied")
    sample_env = staging / SAMPLE_ENV_FILE
    if not sample_env.is_file():
        return []
    return sorted(read_env_keys(sample_env) - read_env_keys(device_env))

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
        subprocess.run(["pm2", "restart","scheduler","app","MQTT_Control_Consumer"], check=True)
        print(f"[info] PM2 service  restarted successfully.")
    except subprocess.CalledProcessError as e:
        print(f"[error] Failed to restart PM2 service: {e}")

# ==============================
# MQTT callbacks
# ==============================
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

        # Check the device .env before touching the live code
        try:
            missing_env_keys = check_device_env(STAGING_DIR, LIVE_CODE_DIR)
        except RuntimeError as e:
            print(f"[error] {e}. The current code keeps running.")
            publish_ack(client, ACK_STATUS_FAILED, reason=str(e))
            del sessions[session_id]
            return
        if missing_env_keys:
            print(f"[warn] Device {DEVICE_ENV_FILE} lacks keys listed in {SAMPLE_ENV_FILE}, "
                  f"the code defaults will be used: {', '.join(missing_env_keys)}")

        # Replace live code
        replace_live_code(STAGING_DIR, LIVE_CODE_DIR)

        publish_ack(client, ACK_STATUS_UPDATED, missing_env_keys=missing_env_keys)

        # Cleanup session
        del sessions[session_id]

        restart_pm2_service()
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