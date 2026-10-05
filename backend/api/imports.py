"""Parse uploaded JSON/CSV and validate the entire proposed snapshot."""

import csv
import io
import json

from backend.data_loader import HISTORY_COLUMNS, DatasetValidationError, reject_duplicate_keys
from backend.models import Dataset
from backend.storage import StateConflict


def parse_upload(name: str, content: bytes):
    try:
        text = content.decode("utf-8-sig")
        if name != "activity_history":
            return json.loads(text, object_pairs_hook=reject_duplicate_keys)
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames or []
        if len(headers) != len(HISTORY_COLUMNS) or set(headers) != set(HISTORY_COLUMNS):
            raise ValueError(f"Expected CSV columns: {', '.join(HISTORY_COLUMNS)}")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"line {reader.line_num}: incorrect number of columns")
            rows.append(row)
        return rows
    except (UnicodeError, ValueError, csv.Error) as exc:
        raise DatasetValidationError(f"{name}: {exc}") from exc


def import_dataset(current: Dataset, parts: dict, mode: str) -> tuple[Dataset, dict]:
    if mode == "replace":
        required = {"employees", "events", "skills", "activity_history"}
        if parts.keys() != required:
            raise DatasetValidationError("replace requires employees, events, skills and activity_history files")
        result = Dataset.model_validate(parts)
    else:
        if not parts or "skills" in parts:
            raise DatasetValidationError("append accepts employees, events and/or activity_history; skills requires replace")
        payload = current.model_dump(mode="json")
        keys = {"employees": "employee_id", "events": "event_id", "activity_history": "record_id"}
        for field, new_items in parts.items():
            if not isinstance(new_items, list):
                raise DatasetValidationError(f"{field}: expected a list")
            old_ids = {item[keys[field]] for item in payload[field]}
            for item in new_items:
                raw_id = item.get(keys[field]) if isinstance(item, dict) else None
                if isinstance(raw_id, str) and raw_id in old_ids:
                    raise StateConflict("duplicate_id", f"{field}: ID '{item[keys[field]]}' already exists")
            payload[field].extend(new_items)
        result = Dataset.model_validate(payload)
    return result, {"status": "imported", "mode": mode, "employees": len(result.employees),
                    "events": len(result.events), "skills": len(result.skills.skill_catalog),
                    "history_records": len(result.activity_history)}


def preview_import(current: Dataset, parts: dict, mode: str) -> dict:
    """Validate an import without writing anything and describe what would change."""
    result, summary = import_dataset(current, parts, mode)
    before = {"employees": {e.employee_id for e in current.employees},
              "events": {e.event_id for e in current.events},
              "activity_history": {r.record_id for r in current.activity_history}}
    after = {"employees": {e.employee_id for e in result.employees},
             "events": {e.event_id for e in result.events},
             "activity_history": {r.record_id for r in result.activity_history}}
    changes = {name: {"added": len(after[name] - before[name]), "removed": len(before[name] - after[name])}
               for name in before}
    return {**summary, "status": "valid", "dry_run": True, "changes": changes,
            "skills_changed": result.skills != current.skills}
