import csv
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac
from pathlib import Path

from .schema import defaults, load_schema
from .settings import settings


TOP_BOX_ITEMS = [
    ("rpps_s_q57", {"9", "10"}, set(), "9 or 10 (best)"),
    ("rpps_s_q1", {"4"}, set(), "Definitely yes"),
    ("rpps_s_q17", {"4"}, set(), "Yes — completely"),
    ("rpps_s_q18", {"4"}, set(), "Yes — completely"),
    ("rpps_s_q19", {"4"}, set(), "Always"),
    ("rpps_s_q20", {"4"}, set(), "Always"),
    ("rpps_s_q21", {"1"}, set(), "Never"),
    ("rpps_s_q68", {"4"}, {"5"}, "Always"),
    ("rpps_s_q22", {"4"}, set(), "Always"),
    ("rpps_s_q23", {"4"}, {"5"}, "Always"),
    ("rpps_s_q24", {"4"}, set(), "Always"),
    ("rpps_s_q25", {"1"}, {"5"}, "Never"),
    ("rpps_s_q69", {"4"}, {"5"}, "Always"),
    ("rpps_s_q67", {"4"}, set(), "Always"),
    ("rpps_s_q70", {"4"}, set(), "Always"),
]

DATA_ELEMENT_GROUPS = [
    ("About the participants", ["rpps_s_q60", "rpps_s_q59", "rpps_s_q62", "rpps_s_q65", "rpps_s_q61", "rpps_s_q63"]),
    ("About the research study", ["rpps_s_q58", "rpps_s_q15", "rpps_s_q66", "rpps_s_q16"]),
    ("About survey fielding", ["sampling", "timing_of_rpps_administration"]),
]

QUESTION_TYPES = {
    "1": ("Participant perception", TOP_BOX_ITEMS),
    "3": ("Reasons for joining a study", [(f"rpps_s_q{n}", {"1"}, set(), "Very important") for n in range(2, 15)]),
    "4": ("Reasons for leaving a study", [(f"rpps_s_q{n}", {"1"}, set(), "Very important") for n in range(26, 40)]),
    "5": ("Reasons for staying in a study", [(f"rpps_s_q{n}", {"1"}, set(), "Very important") for n in range(40, 56)]),
}

FILTER_FIELDS = [
    ("rpps_s_q60", "Age", "About the participants"),
    ("rpps_s_q59", "Education", "About the participants"),
    ("rpps_s_q62", "Ethnicity", "About the participants"),
    ("rpps_s_q65", "Gender", "About the participants"),
    ("rpps_s_q61", "Race", "About the participants"),
    ("rpps_s_q63", "Sex", "About the participants"),
    ("rpps_s_q58", "Demands of study", "About the research study"),
    ("rpps_s_q15", "Disease/disorder to enroll", "About the research study"),
    ("rpps_s_q66", "Informed Consent setting", "About the research study"),
    ("rpps_s_q16", "Study Type", "About the research study"),
    ("sampling", "Sampling approach", "About the survey fielding"),
    ("timing_of_rpps_administration", "Timing of RPPS administration", "About the survey fielding"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240_000)
    return salt, digest.hex()


def verify_password(password: str, salt: str, digest: str) -> bool:
    _, candidate = hash_password(password, salt)
    return secrets.compare_digest(candidate, digest)


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                token TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY,
                record_id TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL UNIQUE,
                language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'not_started',
                response_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                submitted_at TEXT
            );
            """
        )
        existing = conn.execute("SELECT COUNT(*) AS c FROM admins").fetchone()["c"]
        if existing == 0:
            password = os.environ.get("ADMIN_PASSWORD", "changeme")
            salt, digest = hash_password(password)
            conn.execute(
                "INSERT INTO admins (username, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                ("admin", salt, digest, utcnow()),
            )


def authenticate(username: str, password: str):
    with connect() as conn:
        admin = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        if not admin or not verify_password(password, admin["password_salt"], admin["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO admin_sessions (token, admin_id, created_at) VALUES (?, ?, ?)",
            (token, admin["id"], utcnow()),
        )
        return token


def get_admin_by_session(token: str | None):
    if not token:
        return None
    with connect() as conn:
        admin = conn.execute(
            """
            SELECT admins.*, admin_sessions.created_at AS session_created_at FROM admins
            JOIN admin_sessions ON admin_sessions.admin_id = admins.id
            WHERE admin_sessions.token = ?
            """,
            (token,),
        ).fetchone()
        if not admin:
            return None
        created_at = datetime.fromisoformat(admin["session_created_at"])
        if created_at <= datetime.now(timezone.utc) - timedelta(seconds=settings.session_ttl_seconds):
            conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
            return None
        return admin


def delete_session(token: str | None) -> None:
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))


def next_record_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(record_id AS INTEGER)), 0) + 1 AS n "
        "FROM participants WHERE record_id <> '' AND record_id NOT GLOB '*[^0-9]*'"
    ).fetchone()
    return str(row["n"])


def create_participant(language: str = "en", record_id: str | None = None):
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        record_id = record_id or next_record_id(conn)
        token = secrets.token_urlsafe(32)
        now = utcnow()
        values = defaults()
        values["record_id"] = record_id
        values["languages"] = "2" if language == "es" else "1"
        conn.execute(
            """
            INSERT INTO participants
            (record_id, token, language, status, response_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (record_id, token, language, "not_started", json.dumps(values), now, now),
        )
        return conn.execute("SELECT * FROM participants WHERE token = ?", (token,)).fetchone()


def list_participants():
    with connect() as conn:
        return conn.execute("SELECT * FROM participants ORDER BY created_at DESC").fetchall()


def get_participant_by_token(token: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM participants WHERE token = ?", (token,)).fetchone()


def get_participant_values(participant) -> dict:
    if not participant:
        return {}
    values = defaults()
    values.update(json.loads(participant["response_json"] or "{}"))
    return values


def save_participant_response(token: str, values: dict, submitted: bool = False) -> None:
    participant = get_participant_by_token(token)
    if not participant:
        return
    current = get_participant_values(participant)
    current.update(values)
    now = utcnow()
    status = "complete" if submitted or participant["status"] == "complete" else "incomplete"
    submitted_at = now if submitted else participant["submitted_at"]
    with connect() as conn:
        conn.execute(
            """
            UPDATE participants
            SET response_json = ?, status = ?, updated_at = ?, submitted_at = ?
            WHERE token = ?
            """,
            (json.dumps(current, ensure_ascii=False), status, now, submitted_at, token),
        )


def dashboard_stats(question_type: str = "1", filter_field: str = "nofilter") -> dict:
    participants = list_participants()
    values = [get_participant_values(p) for p in participants]
    total = len(participants)
    complete = sum(1 for p in participants if p["status"] == "complete")

    schema = load_schema()

    filter_definitions = [
        {"field": field_name, "label": label, "group": group}
        for field_name, label, group in FILTER_FIELDS
    ]
    allowed_filters = {item["field"] for item in filter_definitions}
    if filter_field not in allowed_filters:
        filter_field = "nofilter"
    if question_type not in (*QUESTION_TYPES.keys(), "2"):
        question_type = "1"

    if filter_field == "nofilter":
        columns = [{"code": "total", "label": "Total"}]
    else:
        filter_schema = schema.by_name[filter_field]
        columns = [{"code": choice.code, "label": choice.label} for choice in filter_schema.choices]

    def rows_for_column(code: str):
        if code == "total":
            return values
        selected_rows = []
        for row in values:
            raw = row.get(filter_field)
            selected = raw if isinstance(raw, list) else [raw]
            if code in selected:
                selected_rows.append(row)
        return selected_rows

    def row_matches_column(row: dict, code: str) -> bool:
        if code == "total":
            return True
        raw = row.get(filter_field)
        selected = raw if isinstance(raw, list) else [raw]
        return code in selected

    analysis_rows = []
    item_definitions = QUESTION_TYPES.get(question_type, ("", []))[1]
    for field_name, positive_codes, na_codes, score_label in item_definitions:
        field = schema.by_name[field_name]
        cells = []
        for column in columns:
            selected_rows = rows_for_column(column["code"])
            submitted = [str(row.get(field_name)) for row in selected_rows if row.get(field_name) not in (None, "", [])]
            not_applicable = sum(value in na_codes for value in submitted)
            applicable = len(submitted) - not_applicable
            positive = sum(value in positive_codes for value in submitted)
            cells.append(
                {
                    "score": round(positive / applicable * 100) if applicable else None,
                    "answered": len(submitted),
                    "missing": len(selected_rows) - len(submitted),
                    "not_applicable": not_applicable,
                    "total": len(selected_rows),
                }
            )
        analysis_rows.append({"field": field_name, "label": field.label, "score_label": score_label, "cells": cells})

    if question_type == "2":
        status_rows = [
            ("Total survey links", None),
            ("Complete", "complete"),
            ("In progress", "incomplete"),
            ("Not started", "not_started"),
            ("Completion rate", "completion_rate"),
        ]
        for label, status in status_rows:
            cells = []
            for column in columns:
                selected_participants = [
                    participant
                    for participant, row in zip(participants, values)
                    if row_matches_column(row, column["code"])
                ]
                if status is None:
                    count = len(selected_participants)
                elif status == "completion_rate":
                    count = sum(p["status"] == "complete" for p in selected_participants)
                else:
                    count = sum(p["status"] == status for p in selected_participants)
                cells.append(
                    {
                        "score": round(count / len(selected_participants) * 100, 1) if status == "completion_rate" and selected_participants else None,
                        "count": count if status != "completion_rate" else None,
                        "total": len(selected_participants),
                    }
                )
            analysis_rows.append({"label": label, "field": "", "score_label": "", "cells": cells})

    data_elements = []
    for group_name, field_names in DATA_ELEMENT_GROUPS:
        group = {"name": group_name, "fields": []}
        for field_name in field_names:
            field = schema.by_name[field_name]
            counts: dict[str, int] = {}
            for row in values:
                raw = row.get(field_name)
                selected = raw if isinstance(raw, list) else ([raw] if raw not in (None, "") else [])
                for code in selected:
                    counts[str(code)] = counts.get(str(code), 0) + 1
            choices = {choice.code: choice.label for choice in field.choices}
            answered = sum(1 for row in values if row.get(field_name) not in (None, "", []))
            group["fields"].append(
                {
                    "field": field_name,
                    "label": field.label,
                    "answered": answered,
                    "distribution": [
                        {
                            "code": code,
                            "label": choices.get(code, code),
                            "count": count,
                            "percent": round(count / answered * 100, 1) if answered else 0,
                        }
                        for code, count in counts.items()
                    ],
                }
            )
        data_elements.append(group)

    return {
        "total": total,
        "complete": complete,
        "incomplete": sum(1 for p in participants if p["status"] == "incomplete"),
        "not_started": sum(1 for p in participants if p["status"] == "not_started"),
        "completion_rate": round((complete / total * 100), 1) if total else 0,
        "question_type": question_type,
        "filter_field": filter_field,
        "question_types": [
            {"value": "1", "label": "Participant perception"},
            {"value": "2", "label": "Response/Completion Rates"},
            {"value": "3", "label": "Reasons for joining a study"},
            {"value": "4", "label": "Reasons for leaving a study"},
            {"value": "5", "label": "Reasons for staying in a study"},
        ],
        "filters": filter_definitions,
        "analysis": {
            "title": "Response/Completion Rates" if question_type == "2" else QUESTION_TYPES[question_type][0],
            "is_response_rates": question_type == "2",
            "columns": columns,
            "rows": analysis_rows,
        },
        "data_elements": data_elements,
    }


def export_rows() -> list[dict[str, str]]:
    schema = load_schema()
    participants = list(reversed(list_participants()))
    rows: list[dict[str, str]] = []
    for participant in participants:
        values = get_participant_values(participant)
        row: dict[str, str] = {}
        for field in schema.fields:
            if field.field_type == "checkbox":
                selected = set(values.get(field.name) or [])
                for choice in field.choices:
                    row[f"{field.name}___{choice.code}"] = "1" if choice.code in selected else "0"
            else:
                value = values.get(field.name, "")
                row[field.name] = "" if value is None else str(value)
        rows.append(row)
    return rows
