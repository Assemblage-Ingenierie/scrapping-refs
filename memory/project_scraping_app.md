---
name: Scraping app architecture project
description: Web app for scraping architecture agency references, validating and uploading to Airtable
type: project
---

Architecture : FastAPI backend (server.py) + Alpine.js/Tailwind frontend (static/index.html).
Airtable base ID : appgFy9XACtzz4v2P, table : "Référence Projet" (tblLfu3fXXpXZBPO0).
Field names in Airtable use snake_case directly (projet, agence, lieu, etc.) — no FIELD_MAP needed.
- agence + maitre_ouvrage = multipleRecordLinks → table "Organisation" (tblidlZfguNTnLFL7), primary field "Nom"
- programme, labels, bet_equipe = multipleSelects (arrays, use typecast:true to auto-create options)
- montant_ht = currency (number, parse "24 000 000 € HT" → 24000000, handle "M€" suffix)
- statut = singleSelect [Livré, Chantier, Étude, Concours]
Airtable API key stored in .env as AIRTABLE_API_KEY.
Anthropic API key in .env but account has no credits — extraction is done rule-based (regex on labeled fields) not via Claude.
Goal : deployable on Vercel eventually (static frontend) + backend on Railway/Render.
Deduplication : read all Airtable records on scraping start, match by "url_fiche" field, PATCH existing vs POST new.

**Why:** User doesn't want to pay for a separate Anthropic API key and the account balance is zero.
**How to apply:** Never suggest Claude API calls in this project — use rule-based text parsing instead.
