from pathlib import Path

# The app.env shipped in every bundle. It is pasted here from the secure store
# before an OTA; the file is git-ignored and never taken from the repo folder
# being published. A device without an app.env gets a copy of it, a device with
# one keeps its own.
APP_ENV_SOURCE_PATH = Path(__file__).resolve().parent / "app.env"
APP_ENV_ARCHIVE_NAME = "app.env"

# Do not put THERMAL_SENSOR in this file. Each device's sensor is set at device
# setup, and shipping the key would change it on every device that takes this
# app.env.


def validate_app_env_source(source_path: Path) -> Path:
    """Return source_path when it exists; raise ValueError otherwise."""
    if not source_path.is_file():
        raise ValueError(f"app.env source missing: {source_path}. Paste it there before publishing.")
    return source_path
