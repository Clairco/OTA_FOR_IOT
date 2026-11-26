"""
Server-side deployment ack collector.

Subscribes to OTA/ack on the publisher's existing paho client, correlates
incoming acks by mac against the resolved target list, and writes a
per-deployment xlsx report under server/reports/.

Attach ordering contract:
    1. publisher calls client.connect() and client.loop_start()
    2. publisher constructs DeploymentAckCollector and calls .attach(client)
    3. publisher then publishes manifest, chunks and complete
    4. publisher calls .wait() (blocks until all expected terminal or timeout)
    5. publisher calls .write_xlsx() and finally client.loop_stop/disconnect

Acks carry no session_id or sha, so correlation is purely by MAC inside the
publish window. Acks from MACs not in expected_targets are dropped.
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook

ACK_TOPIC = "OTA/ack"
ACK_QOS = 2
DEFAULT_ACK_WAIT_SECONDS = 300
MAX_RAW_ACKS_PER_ROW = 10

# Status values devices publish; anything else is treated as not-yet-terminal.
ACK_STATUS_UPDATED = "updated"
ACK_STATUS_FAILED = "failed"
ACK_STATUS_TIMEOUT = "timeout"
_TERMINAL_STATUSES = frozenset({ACK_STATUS_UPDATED, ACK_STATUS_FAILED})

# Payload keys the device sends on each ack. Mirror of the device's ACK_KEY_*.
ACK_PAYLOAD_KEY_MAC = "mac"
ACK_PAYLOAD_KEY_STATUS = "status"
ACK_PAYLOAD_KEY_MODULE = "module"
ACK_PAYLOAD_KEY_STAGE = "stage"
ACK_PAYLOAD_KEY_REASON = "reason"
ACK_PAYLOAD_KEY_SCRIPT_REPLACED = "script_replaced"
ACK_PAYLOAD_KEY_ENV_REPLACED = "env_replaced"
ACK_PAYLOAD_KEY_ENV_PATCH_APPLIED = "env_patch_applied"
ACK_PAYLOAD_KEY_ENV_CHANGES = "env_changes"
ACK_PAYLOAD_KEY_MISSING_ENV_KEYS = "missing_env_keys"
ACK_PAYLOAD_KEY_ENV_PATCH = "env_patch"
ACK_PAYLOAD_KEY_APP_ENV_CREATED = "app_env_created"
ACK_PAYLOAD_KEY_ENV_RESTORED = "env_restored"

# Keys stored in the per-MAC in-memory row. "first_*" captures what the device
# first reported (its intent); "last_*" captures the latest ack (useful for
# retries). The xlsx reports the first-ack snapshot plus the raw list.
ROW_KEY_SENSOR_NAME = "sensor_name"
ROW_KEY_MAC = "mac"
ROW_KEY_FIRST_STATUS = "first_status"
ROW_KEY_FIRST_MODULE = "first_module"
ROW_KEY_FIRST_STAGE = "first_stage"
ROW_KEY_FIRST_REASON = "first_reason"
ROW_KEY_LAST_STATUS = "last_status"
ROW_KEY_LAST_MODULE = "last_module"
ROW_KEY_LAST_STAGE = "last_stage"
ROW_KEY_LAST_REASON = "last_reason"
ROW_KEY_SCRIPT_REPLACED = ACK_PAYLOAD_KEY_SCRIPT_REPLACED
ROW_KEY_ENV_REPLACED = ACK_PAYLOAD_KEY_ENV_REPLACED
ROW_KEY_ENV_PATCH_APPLIED = ACK_PAYLOAD_KEY_ENV_PATCH_APPLIED
ROW_KEY_ENV_CHANGES = ACK_PAYLOAD_KEY_ENV_CHANGES
ROW_KEY_MISSING_ENV_KEYS = ACK_PAYLOAD_KEY_MISSING_ENV_KEYS
ROW_KEY_ENV_PATCH = ACK_PAYLOAD_KEY_ENV_PATCH
ROW_KEY_APP_ENV_CREATED = ACK_PAYLOAD_KEY_APP_ENV_CREATED
ROW_KEY_ENV_RESTORED = ACK_PAYLOAD_KEY_ENV_RESTORED
ROW_KEY_FIRST_ACK_AT_UTC = "first_ack_at_utc"
ROW_KEY_LAST_ACK_AT_UTC = "last_ack_at_utc"
ROW_KEY_ACK_COUNT = "ack_count"
ROW_KEY_RAW_ACKS = "raw_acks"
ROW_KEY_RAW_ACKS_TRUNCATED = "raw_acks_truncated"
ROW_KEY_TERMINAL_SEEN = "_terminal_seen"

# Keys for entries inside the raw-ack list.
RAW_ACK_KEY_RECEIVED_AT = "received_at"
RAW_ACK_KEY_PAYLOAD = "payload"

# Target descriptor keys the publisher hands us in `expected_targets`.
TARGET_KEY_NAME = "name"
TARGET_KEY_MAC = "mac"

# Column order for the xlsx report. Kept in one place so the header row and
# the data row builder cannot drift apart.
XLSX_COLUMN_SENSOR_NAME = "sensor_name"
XLSX_COLUMN_MAC = "mac"
XLSX_COLUMN_FINAL_STATUS = "final_status"
XLSX_COLUMN_FIRST_STATUS = "first_status"
XLSX_COLUMN_LAST_STATUS = "last_status"
XLSX_COLUMN_MODULE = "module"
XLSX_COLUMN_STAGE = "stage"
XLSX_COLUMN_REASON = "reason"
XLSX_COLUMN_SCRIPT_REPLACED = "script_replaced"
XLSX_COLUMN_ENV_REPLACED = "env_replaced"
XLSX_COLUMN_ENV_PATCH_APPLIED = "env_patch_applied"
XLSX_COLUMN_ENV_CHANGES = "env_changes"
XLSX_COLUMN_MISSING_ENV_KEYS = "missing_env_keys"
XLSX_COLUMN_ENV_PATCH = "env_patch"
XLSX_COLUMN_APP_ENV_CREATED = "app_env_created"
XLSX_COLUMN_ENV_RESTORED = "env_restored"
XLSX_COLUMN_FIRST_ACK_AT_UTC = "first_ack_at_utc"
XLSX_COLUMN_LAST_ACK_AT_UTC = "last_ack_at_utc"
XLSX_COLUMN_ACK_COUNT = "ack_count"
XLSX_COLUMN_RAW_ACKS_JSON = "raw_acks_json"
XLSX_COLUMN_RAW_ACKS_TRUNCATED = "raw_acks_truncated"

_XLSX_COLUMNS = (
    XLSX_COLUMN_SENSOR_NAME, XLSX_COLUMN_MAC, XLSX_COLUMN_FINAL_STATUS,
    XLSX_COLUMN_FIRST_STATUS, XLSX_COLUMN_LAST_STATUS,
    XLSX_COLUMN_MODULE, XLSX_COLUMN_STAGE, XLSX_COLUMN_REASON,
    XLSX_COLUMN_SCRIPT_REPLACED, XLSX_COLUMN_ENV_REPLACED,
    XLSX_COLUMN_ENV_PATCH_APPLIED, XLSX_COLUMN_ENV_CHANGES,
    XLSX_COLUMN_MISSING_ENV_KEYS, XLSX_COLUMN_ENV_PATCH,
    XLSX_COLUMN_APP_ENV_CREATED, XLSX_COLUMN_ENV_RESTORED,
    XLSX_COLUMN_FIRST_ACK_AT_UTC, XLSX_COLUMN_LAST_ACK_AT_UTC,
    XLSX_COLUMN_ACK_COUNT, XLSX_COLUMN_RAW_ACKS_JSON,
    XLSX_COLUMN_RAW_ACKS_TRUNCATED,
)

# Workbook layout.
XLSX_SHEET_NAME = "acks"
XLSX_FREEZE_PANES_CELL = "A2"

# Timestamp formats.
UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REPORT_FILENAME_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
# Short session suffix in the filename; the UUID is otherwise noisy.
REPORT_SESSION_SHORT_LEN = 8
REPORT_MISSING_SESSION_PLACEHOLDER = "nosession"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime(UTC_TIMESTAMP_FORMAT)


def _json_or_none(value):
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def _csv_or_none(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


class DeploymentAckCollector:
    def __init__(self, group, session_id, expected_targets, reports_dir,
                 wait_seconds=DEFAULT_ACK_WAIT_SECONDS):
        self.group = group
        self.session_id = session_id
        self.reports_dir = Path(reports_dir)
        self.wait_seconds = int(wait_seconds)

        self._lock = threading.Lock()
        self._all_done = threading.Event()
        self._rows = {}
        self._pending = set()

        for target in expected_targets or []:
            mac = (target.get(TARGET_KEY_MAC) or "").upper()
            if not mac:
                continue
            name = target.get(TARGET_KEY_NAME) or mac
            self._rows[mac] = self._new_row(name, mac)
            self._pending.add(mac)

        if not self._pending:
            self._all_done.set()

    @staticmethod
    def _new_row(sensor_name, mac):
        return {
            ROW_KEY_SENSOR_NAME: sensor_name,
            ROW_KEY_MAC: mac,
            ROW_KEY_FIRST_STATUS: None,
            ROW_KEY_FIRST_MODULE: None,
            ROW_KEY_FIRST_STAGE: None,
            ROW_KEY_FIRST_REASON: None,
            ROW_KEY_LAST_STATUS: None,
            ROW_KEY_LAST_MODULE: None,
            ROW_KEY_LAST_STAGE: None,
            ROW_KEY_LAST_REASON: None,
            ROW_KEY_SCRIPT_REPLACED: None,
            ROW_KEY_ENV_REPLACED: None,
            ROW_KEY_ENV_PATCH_APPLIED: None,
            ROW_KEY_ENV_CHANGES: None,
            ROW_KEY_MISSING_ENV_KEYS: None,
            ROW_KEY_ENV_PATCH: None,
            ROW_KEY_APP_ENV_CREATED: None,
            ROW_KEY_ENV_RESTORED: None,
            ROW_KEY_FIRST_ACK_AT_UTC: None,
            ROW_KEY_LAST_ACK_AT_UTC: None,
            ROW_KEY_ACK_COUNT: 0,
            ROW_KEY_RAW_ACKS: [],
            ROW_KEY_RAW_ACKS_TRUNCATED: False,
            ROW_KEY_TERMINAL_SEEN: False,
        }

    def attach(self, client):
        client.subscribe(ACK_TOPIC, qos=ACK_QOS)
        client.message_callback_add(ACK_TOPIC, self._on_ack)

    def detach(self, client):
        try:
            client.message_callback_remove(ACK_TOPIC)
        except Exception:
            pass
        try:
            client.unsubscribe(ACK_TOPIC)
        except Exception:
            pass

    def wait(self):
        timed_out = not self._all_done.wait(self.wait_seconds)
        with self._lock:
            updated = sum(1 for r in self._rows.values() if r[ROW_KEY_FIRST_STATUS] == ACK_STATUS_UPDATED)
            failed = sum(1 for r in self._rows.values() if r[ROW_KEY_FIRST_STATUS] == ACK_STATUS_FAILED)
            pending = sum(1 for r in self._rows.values() if not r[ROW_KEY_TERMINAL_SEEN])
        return {
            "total": len(self._rows),
            ACK_STATUS_UPDATED: updated,
            ACK_STATUS_FAILED: failed,
            "timed_out_count": pending,
            "timed_out": timed_out,
        }

    def write_xlsx(self, path=None):
        if path is None:
            path = self._default_report_path()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        wb = Workbook()
        ws = wb.active
        ws.title = XLSX_SHEET_NAME
        ws.append(list(_XLSX_COLUMNS))
        ws.freeze_panes = XLSX_FREEZE_PANES_CELL

        with self._lock:
            rows_snapshot = [self._row_to_cells(r) for r in self._rows.values()]

        for row in rows_snapshot:
            ws.append(row)

        wb.save(path)
        return path

    def _default_report_path(self):
        session_source = self.session_id or REPORT_MISSING_SESSION_PLACEHOLDER
        session_short = session_source.replace("-", "")[:REPORT_SESSION_SHORT_LEN]
        ts = datetime.now(timezone.utc).strftime(REPORT_FILENAME_TIMESTAMP_FORMAT)
        filename = f"{self.group}-{session_short}-{ts}.xlsx"
        return self.reports_dir / filename

    def _row_to_cells(self, row):
        final_status = (
            row[ROW_KEY_FIRST_STATUS]
            if row[ROW_KEY_FIRST_STATUS] in _TERMINAL_STATUSES
            else ACK_STATUS_TIMEOUT
        )
        return [
            row[ROW_KEY_SENSOR_NAME],
            row[ROW_KEY_MAC],
            final_status,
            row[ROW_KEY_FIRST_STATUS],
            row[ROW_KEY_LAST_STATUS],
            row[ROW_KEY_FIRST_MODULE],
            row[ROW_KEY_FIRST_STAGE],
            row[ROW_KEY_FIRST_REASON],
            row[ROW_KEY_SCRIPT_REPLACED],
            row[ROW_KEY_ENV_REPLACED],
            row[ROW_KEY_ENV_PATCH_APPLIED],
            _json_or_none(row[ROW_KEY_ENV_CHANGES]),
            _csv_or_none(row[ROW_KEY_MISSING_ENV_KEYS]),
            row[ROW_KEY_ENV_PATCH],
            row[ROW_KEY_APP_ENV_CREATED],
            row[ROW_KEY_ENV_RESTORED],
            row[ROW_KEY_FIRST_ACK_AT_UTC],
            row[ROW_KEY_LAST_ACK_AT_UTC],
            row[ROW_KEY_ACK_COUNT],
            _json_or_none(row[ROW_KEY_RAW_ACKS]) if row[ROW_KEY_RAW_ACKS] else None,
            row[ROW_KEY_RAW_ACKS_TRUNCATED],
        ]

    def _on_ack(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"[warn] ack dropped (malformed payload): {e}")
            return
        if not isinstance(payload, dict):
            return
        mac = (payload.get(ACK_PAYLOAD_KEY_MAC) or "").upper()
        if not mac:
            return
        self._ingest(mac, payload, _now_utc_iso())

    # Payload keys that carry per-device outcome details. Stored on first
    # non-null sighting (`_ingest` doesn't overwrite once set).
    _PAYLOAD_DETAIL_KEYS = (
        ACK_PAYLOAD_KEY_SCRIPT_REPLACED, ACK_PAYLOAD_KEY_ENV_REPLACED,
        ACK_PAYLOAD_KEY_ENV_PATCH_APPLIED, ACK_PAYLOAD_KEY_ENV_CHANGES,
        ACK_PAYLOAD_KEY_MISSING_ENV_KEYS, ACK_PAYLOAD_KEY_ENV_PATCH,
        ACK_PAYLOAD_KEY_APP_ENV_CREATED, ACK_PAYLOAD_KEY_ENV_RESTORED,
    )

    def _ingest(self, mac, payload, ts_iso):
        signal_done = False
        with self._lock:
            row = self._rows.get(mac)
            if row is None:
                # Ack from a MAC outside the expected target set; ignore.
                return

            status = payload.get(ACK_PAYLOAD_KEY_STATUS)
            module = payload.get(ACK_PAYLOAD_KEY_MODULE)
            stage = payload.get(ACK_PAYLOAD_KEY_STAGE)
            reason = payload.get(ACK_PAYLOAD_KEY_REASON)

            is_first = row[ROW_KEY_ACK_COUNT] == 0
            row[ROW_KEY_ACK_COUNT] += 1
            row[ROW_KEY_LAST_ACK_AT_UTC] = ts_iso
            row[ROW_KEY_LAST_STATUS] = status
            row[ROW_KEY_LAST_MODULE] = module
            row[ROW_KEY_LAST_STAGE] = stage
            row[ROW_KEY_LAST_REASON] = reason

            if is_first:
                row[ROW_KEY_FIRST_ACK_AT_UTC] = ts_iso
                row[ROW_KEY_FIRST_STATUS] = status
                row[ROW_KEY_FIRST_MODULE] = module
                row[ROW_KEY_FIRST_STAGE] = stage
                row[ROW_KEY_FIRST_REASON] = reason

            for key in self._PAYLOAD_DETAIL_KEYS:
                if key in payload and row.get(key) in (None, ""):
                    row[key] = payload[key]

            if len(row[ROW_KEY_RAW_ACKS]) < MAX_RAW_ACKS_PER_ROW:
                row[ROW_KEY_RAW_ACKS].append(
                    {RAW_ACK_KEY_RECEIVED_AT: ts_iso, RAW_ACK_KEY_PAYLOAD: payload}
                )
            else:
                row[ROW_KEY_RAW_ACKS_TRUNCATED] = True

            if not row[ROW_KEY_TERMINAL_SEEN] and status in _TERMINAL_STATUSES:
                row[ROW_KEY_TERMINAL_SEEN] = True
                self._pending.discard(mac)
                if not self._pending:
                    signal_done = True

        if signal_done:
            self._all_done.set()
