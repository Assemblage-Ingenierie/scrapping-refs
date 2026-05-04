# Scraping references architecture

Application locale pour scraper des references de projets d'agences d'architecture, les verifier manuellement, puis les envoyer dans Airtable.

Le projet cible le workflow d'Assemblage ingenierie : recuperer rapidement des fiches projets depuis les sites d'agences, normaliser les champs utiles, eviter les doublons par `url_fiche`, et garder une validation humaine avant l'upload.

## Pour humains

### Ce que fait l'application

- charge une liste d'agences depuis Airtable ou depuis une saisie manuelle ;
- detecte et extrait les URLs de fiches projets ;
- permet de previsualiser les URLs et d'analyser les champs source avant scraping ;
- permet de tester les 3 premieres fiches, de verifier le tableau, puis de continuer le scraping ou revenir au mapping ;
- scrape chaque fiche avec `requests` ou Playwright pour les sites rendus en JavaScript ;
- extrait les champs projet via CMS detectes, parsers rule-based, mapping valide, et fallback optionnel Gemini ;
- affiche les resultats dans une interface de validation ;
- permet d'ajuster la largeur des colonnes et de choisir entre champs tronques ou retour a la ligne ;
- cree ou met a jour les fiches dans Airtable.

### Demarrage local

1. Installer les dependances Python :

```powershell
pip install -r requirements.txt
```

2. Installer Chromium pour Playwright si necessaire :

```powershell
python -m playwright install chromium
```

3. Creer `.env` a partir de `.env.example`, puis renseigner au minimum :

```env
AIRTABLE_API_KEY=pat...
AIRTABLE_BASE_ID=app...
GEMINI_API_KEY=... # optionnel, uniquement pour le fallback LLM
GEMINI_MODEL=gemini-3.1-flash-lite-preview # optionnel
```

4. Lancer l'application :

```powershell
uvicorn server:app --reload --port 8000
```

Ou double-cliquer sur `lancer.bat`.

Interface : http://localhost:8000

### Scripts utiles

- `server.py` : backend FastAPI + API Airtable + scraping + parsers.
- `scraper.py` : ancien pipeline CLI avec extraction Claude vers CSV.
- `import_airtable.py` : import CSV historique vers Airtable.
- `agencies.json` et `static/agencies.json` : exemples d'agences connues.
- `memory/project_scraping_app.md` : notes projet existantes a conserver comme contexte.

### Donnees attendues

Les fiches projet manipulent principalement :

- `agence`
- `projet`
- `maitre_ouvrage`
- `mission`
- `programme`
- `rehab_neuf`
- `statut`
- `surface_m2`
- `lieu`
- `annee`
- `montant_ht`
- `labels`
- `bet_equipe`
- `description`
- `image_url`
- `url_fiche`

### Workflow actuel

1. Selectionner une agence connue depuis Airtable ou saisir une agence manuellement.
2. Previsualiser les URLs projet detectees.
3. Analyser les champs source et verifier le mapping propose vers Airtable.
4. Tester les 3 premieres fiches.
5. Si le tableau convient, continuer le scraping depuis la fiche suivante ; sinon revenir au mapping.
6. Valider les lignes selectionnees.
7. Envoyer vers Airtable.

### Gemini

Gemini est optionnel et reste une aide, pas le chemin obligatoire.

Modele par defaut : `gemini-3.1-flash-lite-preview`.

L'interface affiche des compteurs locaux :

- appels du jour : `X / 500` ;
- succes / erreurs ;
- dernier statut et derniere erreur ;
- limites indicatives : 5 requetes/minute et 250k tokens/minute.

Ces compteurs sont locaux a la session serveur. Un redemarrage les remet a zero, contrairement aux quotas reels Google.

Gemini est appele seulement si le toggle LLM est active et si le flux atteint une etape LLM : aide a la detection d'URLs, suggestion de mapping, ou fallback d'extraction. Les sites CMS comme Prismic peuvent etre traites sans appel Gemini.

## Pour IA

Avant de modifier le projet, lire :

1. `AI_CONTRACT.md`
2. `AGENTS.md`
3. `ARCHITECTURE.md`
4. `memory/project_scraping_app.md`

Points essentiels :

- Ne jamais commiter ou afficher les secrets de `.env`.
- Ne pas proposer Claude comme solution active : le projet utilise des parsers rule-based et un fallback Gemini optionnel.
- Preserver la validation humaine avant toute ecriture Airtable.
- Preserver la deduplication par `url_fiche`.
- Les parsers generes doivent rester dans `parsers/` et retourner le schema projet attendu.
- Les parsers sauvegardes peuvent etre supprimes depuis l'interface pour reinitialiser une agence.
- Le champ Airtable d'URL d'agence est `URL page projet`.
