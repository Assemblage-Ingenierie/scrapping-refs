# Architecture

## Vue d'ensemble

Le projet est compose de quatre couches :

1. Interface web statique : `static/index.html`
2. API backend : `server.py`
3. Extraction scraping/parsing : fonctions internes de `server.py` + parsers dans `parsers/`
4. Persistance externe : Airtable

Flux principal :

```text
Utilisateur
  -> static/index.html
  -> FastAPI server.py
  -> site agence
  -> parsing projet
  -> validation humaine
  -> Airtable
```

## Frontend

Fichier : `static/index.html`

Technologies :

- Alpine.js via CDN
- Tailwind CSS via CDN
- HTML statique servi par FastAPI

Responsabilites :

- selection ou saisie d'une agence ;
- previsualisation des URLs candidates avant scraping ;
- analyse du mapping source -> Airtable avant scraping ;
- test des 3 premieres fiches avant scraping complet ;
- lancement du scraping ;
- polling du job backend ;
- affichage des logs et previews ;
- edition manuelle des champs projet ;
- ajustement de largeur de colonnes et bascule retour a la ligne/troncature ;
- marquage creation/mise a jour via comparaison avec Airtable ;
- upload des lignes selectionnees ;
- export CSV local.

Endpoints consommes :

- `GET /api/agencies`
- `GET /api/airtable/records`
- `GET /api/llm/status`
- `POST /api/scrape/preview`
- `POST /api/scrape/analyze-fields`
- `POST /api/scrape/start`
- `GET /api/scrape/{job_id}`
- `POST /api/upsert`

## Backend FastAPI

Fichier : `server.py`

Responsabilites :

- charger `.env` ;
- exposer les endpoints API ;
- servir `static/index.html` ;
- interagir avec Airtable ;
- orchestrer les jobs de scraping en memoire ;
- extraire les URLs projet ;
- parser les fiches projet ;
- charger les parsers sauvegardes.

Les jobs sont stockes dans `jobs: dict[str, dict]`. Ce stockage est volontairement simple et adapte a un usage local/single-user. Un redemarrage serveur efface les jobs en cours.

## Scraping

Deux modes de fetch existent :

- `fetch_static()` avec `requests` pour les pages HTML classiques ;
- `fetch_dynamic()` avec Playwright Chromium pour les sites JavaScript.

`smart_fetch()` commence par `requests`, puis bascule vers Playwright si la page semble tres pauvre en texte ou si `dynamic` est force.

Il n'y a plus d'extracteurs d'URLs dedies a des agences dans `server.py`. Le fallback generique parcourt les liens internes qui ressemblent a des pages detail projet.

Cas particuliers generiques pris en charge :

- ports standards normalises : `https://domaine:443` est traite comme `https://domaine` ;
- sites Indexhibit : les sections `index.php/<section>/...` sont filtrees pour eviter de melanger architecture, urbanisme et autres menus ;
- sites Prismic : le repo Prismic est detecte depuis le HTML, les documents `projet` sont recuperes via l'API Prismic, puis les URLs sont reconstruites depuis les UID ;
- les routes Prismic sont derivees de la page listing, par exemple `/projets/{uid}`.

## Parsing

Le parsing principal est rule-based :

- `parse_project_text()` pour les fiches avec labels classiques ;
- parsers additionnels charges depuis `parsers/*.py`.

Avant le parsing, `extract_page_payload()` construit un payload commun :

- texte principal nettoye depuis `main`, `article` ou un conteneur projet ;
- titre probable depuis `h1`, Open Graph ou `<title>` ;
- image principale avec prise en charge de `src`, `srcset` et attributs lazy-load ;
- metadonnees structurees extraites depuis `dl/dt/dd`, tableaux et lignes `Label: valeur`.

Les lignes inline `Label : valeur` sont traitees en priorite. Exemple :

```text
Maîtrise d'ouvrage : Icade Promotion tertiaire
```

donne le champ source `Maîtrise d'ouvrage` et l'exemple `Icade Promotion tertiaire`.

Le champ `lieu` suit une strategie en cascade :

1. champ explicite `lieu`, `localisation`, `adresse`, `ville` ;
2. inference depuis le titre, par exemple `BRUNOY - ECOLE` -> `Brunoy` ;
3. inference depuis le slug, par exemple `issy-les-moulineaux-construction...` -> `Issy-Les-Moulineaux`.

Le parser generique utilise ces metadonnees avant de retomber sur le texte brut. Les parsers specifiques peuvent accepter `meta` et `title` en arguments supplementaires ; les anciens parsers a quatre arguments restent compatibles.

L'etape de mapping appelle `/api/scrape/analyze-fields` sur un echantillon de fiches. Elle retourne les champs source detectes, avec exemples, cible Airtable proposee et confiance. L'interface envoie ensuite `field_mapping` au lancement du scraping. Le backend accepte les deux formats pour compatibilite :

- `{source_site: champ_airtable}` ;
- `{champ_airtable: source_site}`.

Le fallback LLM Gemini est optionnel :

- active seulement si `GEMINI_API_KEY` est configuree ;
- modele par defaut : `gemini-3.1-flash-lite-preview`, surchargeable par `GEMINI_MODEL` ;
- utile pour une agence inconnue ou un mapping ambigu ;
- suivi par des compteurs locaux exposes dans `/api/llm/status` ;
- peut generer un parser Python sauvegarde dans `parsers/`.

Gemini n'est pas force par le toggle. Le toggle autorise son usage. Les chemins deterministes restent prioritaires : CMS, heuristiques, mapping valide et parsers locaux.

Les parsers doivent retourner le schema complet :

```python
{
    "agence": agency,
    "projet": "",
    "maitre_ouvrage": "",
    "mission": "",
    "programme": "Autre",
    "rehab_neuf": ["neuf"],
    "statut": "Étude",
    "surface_m2": None,
    "lieu": "",
    "annee": None,
    "montant_ht": "",
    "labels": "",
    "bet_equipe": "",
    "description": "",
    "image_url": image_url,
    "url_fiche": url,
    "_selected": True,
    "_is_update": False,
    "_airtable_id": None,
}
```

## Airtable

Tables principales :

- `Reference Projet` ou nom configure par `AIRTABLE_TABLE`
- `Organisation`

Champs particuliers :

- `agence` est un lien vers `Organisation`.
- `maitre_ouvrage` est envoye comme texte simple.
- `programme`, `labels`, `bet_equipe` et `rehab_neuf` sont envoyes comme listes pour des champs multi-select.
- `rehab_neuf` est envoye vers le champ Airtable `rehab/neuf`.
- l'URL projet d'une agence est lue depuis le champ Organisation `URL page projet`, avec fallback sur `URL site` puis `Site web`.
- `surface_m2` et `annee` sont convertis en entiers.
- `montant_ht` est parse en nombre quand possible.

Deduplication :

1. Le frontend charge les fiches existantes via `/api/airtable/records`.
2. Il compare `url_fiche`.
3. Il renseigne `_airtable_id` si une fiche existe.
4. `/api/upsert` fait un `PATCH` si `_airtable_id` existe, sinon un `POST`.

## Scripts historiques

`scraper.py` contient un pipeline CLI anterieur oriente CSV et Claude. Il reste utile comme reference de scraping, mais le backend actif est `server.py`.

`import_airtable.py` importe un CSV vers Airtable avec un mapping de champs plus ancien. Le flux actuel prefere l'upload direct depuis l'interface.

Les parsers sauvegardes dans `parsers/` sont listables via `/api/parsers` et supprimables via `DELETE /api/parsers/{filename}` pour reinitialiser une agence.
