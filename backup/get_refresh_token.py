#!/usr/bin/env python3
"""
Engangsskript for å hente Google OAuth2 refresh token til backup-scriptet.

Kjør lokalt (IKKE på Railway):
    pip install google-auth-oauthlib
    python backup/get_refresh_token.py

Du trenger client_secret.json fra Google Cloud Console:
    APIs & Services → Credentials → din OAuth 2.0-klient → Last ned JSON
    Legg filen i samme mappe som dette scriptet (backup/client_secret.json)

Kopier verdiene som skrives ut til Railway-variablene:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
"""

from google_auth_oauthlib.flow import InstalledAppFlow
import os

SCOPES = ["https://www.googleapis.com/auth/drive"]
SECRET_FILE = os.path.join(os.path.dirname(__file__), "client_secret.json")

if not os.path.exists(SECRET_FILE):
    print(f"FEIL: Finner ikke {SECRET_FILE}")
    print("Last ned client_secret.json fra Google Cloud Console og legg den i backup/-mappen.")
    exit(1)

flow = InstalledAppFlow.from_client_secrets_file(SECRET_FILE, SCOPES)
creds = flow.run_local_server(port=0)

print("\n✅ Autentisering vellykket! Kopier disse til Railway-variablene:\n")
print(f"GOOGLE_CLIENT_ID     = {creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET = {creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN = {creds.refresh_token}")
