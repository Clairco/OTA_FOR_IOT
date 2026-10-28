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
    # 1. Delete everything in the live folder
    if live.exists():
        print(f"[info] Deleting old code in {live}")
        for item in live.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
            else:
                shutil.rmtree(item)
    else:
        live.mkdir(parents=True)

    # 2. Move all files from staging to live
    print(f"[info] Moving new code from {staging} to {live}")
    for item in staging.iterdir():
        shutil.move(str(item), str(live / item.name))

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

        # Replace live code
        replace_live_code(STAGING_DIR, LIVE_CODE_DIR)

        client.publish(f"OTA/ack", payload=json.dumps({"mac_Id":MAC_ADDRESS,"status": "updated"}), qos=2)

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
