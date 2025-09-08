#!/usr/bin/env python3
import subprocess
import logging
import os
import sys
from datetime import datetime

# === CONFIG ===
SCRIPTS = ["test.py"]   # List of Python scripts
REPO_DIR = "/home/parshant/OTA_FOR_IOT"  # Path to your git repo
BRANCH = "main"  # Branch to track
LOG_FILE = "/var/log/git_autopull_pm2.log"

# === LOGGING SETUP ===
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

def run_cmd(cmd, cwd=None, check=False):
    """Run a shell command and log its output."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=check
        )
        if result.stdout:
            logging.info(result.stdout.strip())
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {cmd}\n{e.output}")
        if check:
            sys.exit(1)
        return None

def main():
    if not os.path.isdir(REPO_DIR):
        logging.error(f"Repo directory not found: {REPO_DIR}")
        sys.exit(1)

    logging.info("==== Git Auto Pull PM2 Script Started ====")

    # Fetch latest changes
    run_cmd("git fetch origin", cwd=REPO_DIR)

    local_commit = run_cmd(f"git rev-parse {BRANCH}", cwd=REPO_DIR)
    remote_commit = run_cmd(f"git rev-parse origin/{BRANCH}", cwd=REPO_DIR)

    if local_commit != remote_commit:
        logging.info("Changes detected. Updating...")

        # Pull latest code
        run_cmd(f"git reset --hard origin/{BRANCH}", cwd=REPO_DIR)

        # Install dependencies if requirements.txt exists
        req_file = os.path.join(REPO_DIR, "requirements.txt")
        if os.path.exists(req_file):
            run_cmd(f"python3 -m pip install -r requirements.txt", cwd=REPO_DIR)

        # Restart all scripts with PM2
        for script in SCRIPTS:
            name = os.path.splitext(os.path.basename(script))[0]

            # Stop existing process if running
            pm2_list = run_cmd("pm2 list")
            if pm2_list and name in pm2_list:
                run_cmd(f"pm2 stop {name}")

            # Start script with PM2
            run_cmd(f"pm2 start {script} --interpreter python3 --name {name}", cwd=REPO_DIR)

        # Save PM2 process list
        run_cmd("pm2 save")

        logging.info("All scripts restarted with new changes.")
    else:
        logging.info("No changes found.")

if __name__ == "__main__":
    main()
