#!/bin/bash

# === CONFIG ===
SCRIPTS=("test.py")   # List of Python scripts
REPO_DIR="/home/parshant/OTA_FOR_IOT"               # Path to your git repo
BRANCH="main"                               # Branch to track
LOG_FILE="/var/log/git_autopull_pm2.log"

# === SCRIPT ===
cd "$REPO_DIR" || exit 1

echo "==== $(date) ====" >> "$LOG_FILE"

# Fetch latest changes
git fetch origin >> "$LOG_FILE" 2>&1

LOCAL_COMMIT=$(git rev-parse "$BRANCH")
REMOTE_COMMIT=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL_COMMIT" != "$REMOTE_COMMIT" ]; then
    echo "Changes detected. Updating..." >> "$LOG_FILE"

    # Pull latest code
    git reset --hard "origin/$BRANCH" >> "$LOG_FILE" 2>&1

    # Install Python dependencies if requirements.txt exists
    if [ -f "requirements.txt" ]; then
        python3 -m pip install -r requirements.txt >> "$LOG_FILE" 2>&1
    fi

    # Restart all scripts in SCRIPTS[]
    for SCRIPT in "${SCRIPTS[@]}"; do
        NAME=$(basename "$SCRIPT" .py)   # PM2 process name = script name without .py

        # Stop existing process if running
        if pm2 list | grep -q "$NAME"; then
            pm2 stop "$NAME" >> "$LOG_FILE" 2>&1
        fi

        # Start (or restart) script with PM2
        pm2 start "$SCRIPT" --interpreter python3 --name "$NAME" >> "$LOG_FILE" 2>&1
    done

    # Save PM2 process list so it restarts on reboot
    pm2 save >> "$LOG_FILE" 2>&1

    echo "All scripts restarted with new changes." >> "$LOG_FILE"
else
    echo "No changes found." >> "$LOG_FILE"
fi
