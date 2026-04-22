#!/usr/bin/env python3
"""
BrickHaus – Database backup script
Exports all Supabase tables to a compressed JSON file and uploads to Google Drive.

Usage:
    python backup/backup.py

Required environment variables:
    SUPABASE_URL           – Supabase project URL
    SUPABASE_SERVICE_KEY   – Supabase service role key
    GOOGLE_CLIENT_ID       – OAuth2 client ID
    GOOGLE_CLIENT_SECRET   – OAuth2 client secret
    GOOGLE_REFRESH_TOKEN   – OAuth2 refresh token (obtained via backup/get_refresh_token.py)
"""

import gzip
import json
import os
import tempfile
from datetime import date, datetime, timezone

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL      = os.environ["SUPABASE_URL"].strip().rstrip("/")
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"].strip()
GOOGLE_CLIENT_ID  = os.environ["GOOGLE_CLIENT_ID"].strip()
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"].strip()
GOOGLE_REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"].strip()

DRIVE_FOLDER_ID = "1OW8kPSr0uv-hWurbiv89RibY49V0087l"   # BrickHaus/Backups
TABLES          = ["objects", "locations", "tags", "images"]
KEEP_BACKUPS    = 8   # keep last 8 weekly backups (~2 months)

# ── Supabase helpers ──────────────────────────────────────────────────────────

def fetch_table(table: str) -> list:
    """Fetch all rows from a Supabase table using pagination."""
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept":        "application/json",
        "Range-Unit":    "items",
    }
    rows      = []
    page_size = 1000
    offset    = 0

    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**headers, "Range": f"{offset}-{offset + page_size - 1}"},
            params={"select": "*", "order": "id"},
            timeout=60,
        )
        resp.raise_for_status()
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    return rows

# ── Google Drive helpers ──────────────────────────────────────────────────────

def build_drive_service():
    """Build Drive service using OAuth2 user credentials (refresh token)."""
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_to_drive(service, filepath: str, filename: str) -> str:
    """Upload file to backup folder. Returns Drive file ID."""
    file_metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(filepath, mimetype="application/gzip", resumable=True)
    f = service.files().create(
        body=file_metadata, media_body=media, fields="id"
    ).execute()
    return f["id"]


def cleanup_old_backups(service) -> int:
    """Delete backups beyond KEEP_BACKUPS, newest first. Returns count deleted."""
    result = service.files().list(
        q=(
            f"'{DRIVE_FOLDER_ID}' in parents"
            " and name contains 'brickhaus-backup-'"
            " and trashed = false"
        ),
        orderBy="createdTime desc",
        fields="files(id, name)",
        pageSize=50,
    ).execute()

    files     = result.get("files", [])
    to_delete = files[KEEP_BACKUPS:]
    for f in to_delete:
        service.files().delete(fileId=f["id"]).execute()
        print(f"  Deleted old backup: {f['name']}")
    return len(to_delete)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = date.today().isoformat()
    print(f"=== BrickHaus backup – {today} ===")

    # 1. Export all tables
    backup = {
        "backup_date":       today,
        "backup_created_at": datetime.now(timezone.utc).isoformat(),
        "project":           "BrickHaus",
        "supabase_url":      SUPABASE_URL,
        "tables":            {},
    }
    total_rows = 0
    for table in TABLES:
        rows = fetch_table(table)
        backup["tables"][table] = rows
        total_rows += len(rows)
        print(f"  {table}: {len(rows)} rows")

    # 2. Compress to temp file
    filename = f"brickhaus-backup-{today}.json.gz"
    with tempfile.NamedTemporaryFile(suffix=".json.gz", delete=False) as tmp:
        tmp_path = tmp.name

    with gzip.open(tmp_path, "wt", encoding="utf-8") as gz:
        json.dump(backup, gz, ensure_ascii=False, default=str)

    size_kb = os.path.getsize(tmp_path) // 1024
    print(f"  Compressed size: {size_kb} KB")

    # 3. Upload to Google Drive
    service  = build_drive_service()
    file_id  = upload_to_drive(service, tmp_path, filename)
    print(f"  Uploaded to Drive: {filename} (id={file_id})")

    # 4. Clean up old backups
    deleted = cleanup_old_backups(service)
    if deleted:
        print(f"  Removed {deleted} old backup(s)")

    os.unlink(tmp_path)
    print(f"=== Done. {total_rows} rows exported. ===")


if __name__ == "__main__":
    main()
