"""
Import CSV de projets → table Airtable "Références".
Usage:
  python import_airtable.py projets.csv
  python import_airtable.py projets.csv --table "Références" --base appXXXXXXXXXXXXXX
"""

import argparse
import csv
import os
import sys
import time

import requests

AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "app7d0qSY8L7om0Ke")
AIRTABLE_TABLE   = "Références"
BATCH_SIZE       = 10  # Airtable max records per request

# Mapping CSV field → Airtable field name
# Adjust if your Airtable column names differ
FIELD_MAP = {
    "agence":        "Agence",
    "projet":        "Projet",
    "maitre_ouvrage": "Maître d'ouvrage",
    "programme":     "Programme",
    "statut":        "Statut",
    "surface_m2":    "Surface (m²)",
    "lieu":          "Lieu",
    "annee":         "Année",
    "montant_ht":    "Montant HT",
    "labels":        "Labels",
    "bet_equipe":    "BET / Équipe",
    "description":   "Description",
    "image_url":     "Image URL",
    "url_fiche":     "URL fiche",
}

# Fields that should be cast to integers in Airtable (Number type)
INT_FIELDS = {"surface_m2", "annee"}


def load_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def build_airtable_record(row: dict) -> dict:
    fields = {}
    for csv_key, at_key in FIELD_MAP.items():
        val = row.get(csv_key, "").strip()
        if not val or val.lower() == "null":
            continue
        if csv_key in INT_FIELDS:
            try:
                val = int(float(val))
            except (ValueError, TypeError):
                continue
        fields[at_key] = val
    return {"fields": fields}


def push_to_airtable(
    records: list[dict],
    base_id: str,
    table: str,
    api_key: str,
):
    url = f"https://api.airtable.com/v0/{base_id}/{requests.utils.quote(table)}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    created = 0
    errors  = 0

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        payload = {"records": batch}
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        if resp.ok:
            created += len(batch)
            print(f"  [OK] {created}/{len(records)} enregistrements créés")
        else:
            errors += len(batch)
            print(f"  [ERR] Batch {i//BATCH_SIZE + 1}: {resp.status_code} — {resp.text[:200]}")
        time.sleep(0.25)  # respect Airtable rate limit (5 req/s)

    print(f"\n[Airtable] Terminé — {created} créés, {errors} erreurs")


def main():
    parser = argparse.ArgumentParser(description="Import CSV → Airtable")
    parser.add_argument("csv_file", help="Fichier CSV à importer")
    parser.add_argument("--base",  default=AIRTABLE_BASE_ID, help="ID de la base Airtable")
    parser.add_argument("--table", default=AIRTABLE_TABLE,   help="Nom de la table Airtable")
    args = parser.parse_args()

    api_key = os.environ.get("AIRTABLE_API_KEY") or os.environ.get("AIRTABLE_TOKEN")
    if not api_key:
        sys.exit("Erreur : AIRTABLE_API_KEY ou AIRTABLE_TOKEN non défini dans l'environnement.")

    rows = load_csv(args.csv_file)
    print(f"[CSV] {len(rows)} ligne(s) lues depuis {args.csv_file}")

    records = [build_airtable_record(r) for r in rows]
    records = [r for r in records if r["fields"]]  # skip empty rows
    print(f"[Airtable] {len(records)} enregistrement(s) à envoyer vers '{args.table}'")

    push_to_airtable(records, args.base, args.table, api_key)


if __name__ == "__main__":
    main()
