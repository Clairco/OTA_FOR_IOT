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

_TERMINAL_STATUSES = {"updated", "failed"}

# Column order for the xlsx report. Kept in one place so the header row and
# the data row builder cannot drift apart.
_XLSX_COLUMNS = (
    "sensor_name",
    "mac",
    "final_status",
    "first_status",
    "last_status",
    "module",
    "stage",
    "reason",
    "script_replaced",
    "env_replaced",
    "env_patch_applied",
    "env_changes",
    "missing_env_keys",
    "env_patch",
    "app_env_created",
    "env_restored",
    "first_ack_at_utc",
    "last_ack_at_utc",
    "ack_count",
    "raw_acks_json",
    "raw_acks_truncated",
)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
            mac = (target.get("mac") or "").upper()
            if not mac:
                continue
            name = target.get("name") or mac
            self._rows[mac] = self._new_row(name, mac)
            self._pending.add(mac)

        if not self._pending:
            self._all_done.set()

    @staticmethod
    def _new_row(sensor_name, mac):
        return {
            "sensor_name": sensor_name,
            "mac": mac,
            "first_status": None,
            "first_module": None,
            "first_stage": None,
            "first_reason": None,
            "last_status": None,
            "last_module": None,
            "last_stage": None,
            "last_reason": None,
            "script_replaced": None,
            "env_replaced": None,
            "env_patch_applied": None,
            "env_changes": None,
            "missing_env_keys": None,
            "env_patch": None,
            "app_env_created": None,
            "env_restored": None,
            "first_ack_at_utc": None,
            "last_ack_at_utc": None,
            "ack_count": 0,
            "raw_acks": [],
            "raw_acks_truncated": False,
            "_terminal_seen": False,
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
            updated = sum(1 for r in self._rows.values() if r["first_status"] == "updated")
            failed = sum(1 for r in self._rows.values() if r["first_status"] == "failed")
            pending = sum(1 for r in self._rows.values() if not r["_terminal_seen"])
        return {
            "total": len(self._rows),
            "updated": updated,
            "failed": failed,
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
        ws.title = "acks"
        ws.append(list(_XLSX_COLUMNS))
        ws.freeze_panes = "A2"

        with self._lock:
            rows_snapshot = [self._row_to_cells(r) for r in self._rows.values()]

        for row in rows_snapshot:
            ws.append(row)

        wb.save(path)
        return path

    def _default_report_path(self):
        session_short = (self.session_id or "nosession").replace("-", "")[:8]
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{self.group}-{session_short}-{ts}.xlsx"
        return self.reports_dir / filename

    def _row_to_cells(self, row):
        final_status = (
            row["first_status"]
            if row["first_status"] in _TERMINAL_STATUSES
            else "timeout"
        )
        return [
            row["sensor_name"],
            row["mac"],
            final_status,
            row["first_status"],
            row["last_status"],
            row["first_module"],
            row["first_stage"],
            row["first_reason"],
            row["script_replaced"],
            row["env_replaced"],
            row["env_patch_applied"],
            _json_or_none(row["env_changes"]),
            _csv_or_none(row["missing_env_keys"]),
            row["env_patch"],
            row["app_env_created"],
            row["env_restored"],
            row["first_ack_at_utc"],
            row["last_ack_at_utc"],
            row["ack_count"],
            _json_or_none(row["raw_acks"]) if row["raw_acks"] else None,
            row["raw_acks_truncated"],
        ]

    def _on_ack(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"[warn] ack dropped (malformed payload): {e}")
            return
        if not isinstance(payload, dict):
            return
        mac = (payload.get("mac") or "").upper()
        if not mac:
            return
        self._ingest(mac, payload, _now_utc_iso())

    def _ingest(self, mac, payload, ts_iso):
        signal_done = False
        with self._lock:
            row = self._rows.get(mac)
            if row is None:
                # Ack from a MAC outside the expected target set; ignore.
                return

            status = payload.get("status")
            is_first = row["ack_count"] == 0
            row["ack_count"] += 1
            row["last_ack_at_utc"] = ts_iso
            row["last_status"] = status
            row["last_module"] = payload.get("module")
            row["last_stage"] = payload.get("stage")
            row["last_reason"] = payload.get("reason")

            if is_first:
                row["first_ack_at_utc"] = ts_iso
                row["first_status"] = status
                row["first_module"] = payload.get("module")
                row["first_stage"] = payload.get("stage")
                row["first_reason"] = payload.get("reason")

            for key in (
                "script_replaced", "env_replaced", "env_patch_applied",
                "env_changes", "missing_env_keys", "env_patch",
                "app_env_created", "env_restored",
            ):
                if key in payload and row.get(key) in (None, ""):
                    row[key] = payload[key]

            if len(row["raw_acks"]) < MAX_RAW_ACKS_PER_ROW:
                row["raw_acks"].append({"received_at": ts_iso, "payload": payload})
            else:
                row["raw_acks_truncated"] = True

            if not row["_terminal_seen"] and status in _TERMINAL_STATUSES:
                row["_terminal_seen"] = True
                self._pending.discard(mac)
                if not self._pending:
                    signal_done = True

        if signal_done:
            self._all_done.set()
