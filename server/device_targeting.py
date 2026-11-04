"""
Resolve the OTA target device set from a config file and MongoDB.

The config file lists device names and a mode (all / include / exclude). This
module reads it, looks up each name in MongoDB to get its MAC address, works
out the effective target set, and builds the smallest 'targets' block the
publisher embeds in the manifest. It also prints a human-readable summary and
asks for interactive confirmation before the publisher sends anything.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TARGET_MODE_ALL = "all"
TARGET_MODE_INCLUDE = "include"
TARGET_MODE_EXCLUDE = "exclude"
VALID_MODES = frozenset({TARGET_MODE_ALL, TARGET_MODE_INCLUDE, TARGET_MODE_EXCLUDE})

CONFIG_KEY_MODE = "mode"
CONFIG_KEY_DEVICES = "devices"

MANIFEST_KEY_MODE = "mode"
MANIFEST_KEY_MAC = "mac"

MONGO_FIELD_NAME = "name"
MONGO_FIELD_MAC = "mac"
MONGO_FIELD_GROUP = "device_group"

MONGO_SERVER_SELECTION_TIMEOUT_MS = 5000

CONFIRM_YES = frozenset({"y", "yes"})


class TargetingError(Exception):
    """Raised when the targeting config, Mongo lookup, or resolution fails."""


def load_targets_config(path: Path) -> dict:
    """Read and validate targets.config.json. Returns {mode, devices}."""
    if not path.is_file():
        raise TargetingError(f"targets config missing: {path}")
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise TargetingError(f"targets config is not valid JSON: {e}") from e
    if not isinstance(config, dict):
        raise TargetingError("targets config must be a JSON object")

    mode = config.get(CONFIG_KEY_MODE)
    if mode not in VALID_MODES:
        raise TargetingError(
            f"targets config '{CONFIG_KEY_MODE}' must be one of {sorted(VALID_MODES)}, got {mode!r}"
        )

    devices_raw = config.get(CONFIG_KEY_DEVICES, [])
    if mode == TARGET_MODE_ALL:
        devices = []
    else:
        if not isinstance(devices_raw, list) or not devices_raw:
            raise TargetingError(
                f"targets config needs a non-empty '{CONFIG_KEY_DEVICES}' list for mode '{mode}'"
            )
        if not all(isinstance(d, str) and d.strip() for d in devices_raw):
            raise TargetingError(
                f"targets config '{CONFIG_KEY_DEVICES}' must be a list of non-empty strings"
            )
        devices = [d.strip() for d in devices_raw]

    return {CONFIG_KEY_MODE: mode, CONFIG_KEY_DEVICES: devices}


def fetch_group_devices(mongo_uri: str, db_name: str, collection_name: str, device_group: str) -> list:
    """Return [{name, mac}, ...] for every device in the given group."""
    try:
        from pymongo import MongoClient
    except ImportError as e:
        raise TargetingError("pymongo is required; install it with: pip install pymongo") from e

    client = None
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS)
        collection = client[db_name][collection_name]
        docs = list(collection.find(
            {MONGO_FIELD_GROUP: device_group},
            {MONGO_FIELD_NAME: 1, MONGO_FIELD_MAC: 1, "_id": 0},
        ))
    except Exception as e:
        raise TargetingError(f"MongoDB query failed: {e}") from e
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    devices = []
    for doc in docs:
        name = doc.get(MONGO_FIELD_NAME)
        mac = doc.get(MONGO_FIELD_MAC)
        if not name or not mac:
            print(f"[warn] skipping malformed Mongo doc (missing name or mac): {doc}", file=sys.stderr)
            continue
        devices.append({"name": name, "mac": str(mac).upper()})
    return devices


def resolve_targets(config: dict, group_devices: list) -> dict:
    """
    Resolve config names to Mongo devices and compute the effective MAC set.

    Raises TargetingError when a name in the config has no match, or when the
    group has duplicate names (a data bug we won't paper over).
    """
    mode = config[CONFIG_KEY_MODE]

    by_name = {}
    for device in group_devices:
        if device["name"] in by_name:
            raise TargetingError(
                f"MongoDB has multiple devices named '{device['name']}' in this group"
            )
        by_name[device["name"]] = device

    requested = []
    seen = set()
    unknown = []
    for name in config[CONFIG_KEY_DEVICES]:
        if name in seen:
            continue
        seen.add(name)
        match = by_name.get(name)
        if match is None:
            unknown.append(name)
        else:
            requested.append(match)
    if unknown:
        raise TargetingError(
            f"these names are not in MongoDB for this group: {', '.join(unknown)}"
        )

    group_macs = [d["mac"] for d in group_devices]
    requested_mac_set = {d["mac"] for d in requested}

    if mode == TARGET_MODE_ALL:
        effective_macs = list(group_macs)
    elif mode == TARGET_MODE_INCLUDE:
        effective_macs = [d["mac"] for d in requested]
    else:  # exclude
        effective_macs = [m for m in group_macs if m not in requested_mac_set]

    return {
        "config_mode": mode,
        "group_devices": group_devices,
        "group_macs": group_macs,
        "requested_devices": requested,
        "effective_macs": effective_macs,
    }


def build_manifest_targets(resolved: dict) -> dict:
    """Pick the smaller of include/exclude for the manifest payload."""
    mode = resolved["config_mode"]
    if mode == TARGET_MODE_ALL:
        return {MANIFEST_KEY_MODE: TARGET_MODE_ALL}

    effective_set = set(resolved["effective_macs"])
    exclude_macs = [m for m in resolved["group_macs"] if m not in effective_set]
    include_macs = list(resolved["effective_macs"])

    if len(include_macs) <= len(exclude_macs):
        return {MANIFEST_KEY_MODE: TARGET_MODE_INCLUDE, MANIFEST_KEY_MAC: include_macs}
    return {MANIFEST_KEY_MODE: TARGET_MODE_EXCLUDE, MANIFEST_KEY_MAC: exclude_macs}


def confirm_deployment(resolved: dict, device_group: str, manifest_targets: dict) -> bool:
    """Print a summary and read 'y' / 'yes' from the terminal."""
    mode = resolved["config_mode"]
    group_size = len(resolved["group_macs"])
    effective_count = len(resolved["effective_macs"])
    manifest_mode = manifest_targets[MANIFEST_KEY_MODE]
    manifest_list_size = len(manifest_targets.get(MANIFEST_KEY_MAC, []))

    print()
    print("OTA deployment plan")
    print("-" * 60)
    print(f"  Device group:    {device_group}  ({group_size} device(s) in group)")
    print(f"  Config mode:     {mode}")
    manifest_detail = "" if manifest_mode == TARGET_MODE_ALL else f"  ({manifest_list_size} MAC(s) in list)"
    print(f"  Manifest mode:   {manifest_mode}{manifest_detail}")
    print(f"  Will deploy to:  {effective_count} device(s)")
    print()

    if mode == TARGET_MODE_ALL:
        print("  Every device in the group is targeted.")
    else:
        label = "Devices to include" if mode == TARGET_MODE_INCLUDE else "Devices to exclude"
        print(f"  {label} (from config):")
        for i, device in enumerate(resolved["requested_devices"], 1):
            print(f"    {i:3}. {device['name']:<30}  {device['mac']}")
    print()

    try:
        answer = input("Proceed with deployment? [y/N]: ").strip().lower()
    except EOFError:
        answer = ""
    return answer in CONFIRM_YES


def write_resolved_file(
    path: Path,
    device_group: str,
    resolved: dict,
    manifest_targets: dict,
    archive_filename: str,
) -> None:
    """Record what was actually sent, for post-mortem lookups."""
    data = {
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "device_group": device_group,
        "config_mode": resolved["config_mode"],
        "group_size": len(resolved["group_macs"]),
        "effective_target_count": len(resolved["effective_macs"]),
        "requested_devices": resolved["requested_devices"],
        "effective_macs": resolved["effective_macs"],
        "manifest_targets": manifest_targets,
        "archive_filename": archive_filename,
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
