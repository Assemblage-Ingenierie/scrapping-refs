# Instructions Codex

Ce fichier decrit comment travailler dans ce dossier avec Codex.

## Priorites

1. Respecter `AI_CONTRACT.md`.
2. Garder les changements petits, lisibles et compatibles avec l'existant.
3. Preserver le workflow utilisateur : scraping, validation, upload Airtable.
4. Ne pas transformer ce prototype en grosse plateforme sans demande explicite.

## Contexte projet

Le projet est une application Python/FastAPI avec frontend statique Alpine.js/Tailwind. Elle scrape des references de projets d'architecture, normalise les champs, puis permet de les envoyer dans Airtable.

Le backend principal est `server.py`. Le fichier `scraper.py` est un pipeline CLI historique qui utilise Claude ; ne pas s'en inspirer pour ajouter de nouvelles fonctionnalites LLM sans verifier le contrat IA.

## Regles de modification

- Ne jamais lire, imprimer, copier ou documenter les valeurs reelles de `.env`.
- Ne pas supprimer les parsers existants dans `parsers/`.
- Ne pas casser les noms de champs snake_case utilises par Airtable dans `server.py`.
- Ne pas renommer les endpoints API sans mettre a jour `static/index.html`.
- Ne pas bypasser l'ecran de validation frontend avant `/api/upsert`.
- Ne pas ajouter de dependance lourde si `requests`, BeautifulSoup, Playwright, FastAPI ou du code local suffisent.
- Garder le mode "tester les 3 premiers" avant scraping complet.
- Garder Gemini optionnel : le toggle autorise Gemini mais ne doit pas remplacer les chemins deterministes.
- Garder les compteurs Gemini visibles et ne jamais afficher la cle API.

## Style de code

- Python simple et explicite.
- Garder les fonctions de scraping tolerantes aux erreurs reseau et aux pages mal structurees.
- Pour une nouvelle agence connue, preferer :
  - une heuristique generique si possible, par exemple normalisation d'URL, CMS detecte, ou structure Indexhibit ;
  - un extracteur d'URLs dedie dans `server.py` seulement si le listing a une structure particuliere et non generalisable ;
  - un parser dedie dans `parsers/<agence>.py` si le texte projet a une structure stable.
- Les parsers doivent retourner tous les champs du schema, meme vides.
- Les paires `Label : valeur` doivent rester separees : le label va dans `source_label`, la valeur dans `sample_values`.

## Verification minimale

Apres un changement Python :

```powershell
python -m py_compile server.py scraper.py import_airtable.py
```

Apres un changement frontend :

- lancer `uvicorn server:app --reload --port 8000` ;
- ouvrir http://localhost:8000 ;
- verifier le chargement de l'interface, le statut Airtable/LLM et au moins un flux de scraping si les cles sont disponibles.

Apres un changement sur parsing ou URL detection :

```powershell
python -m unittest tests.test_parsing
```

Tester au moins un cas reel pertinent via `/api/scrape/preview` ou `/api/scrape/analyze-fields` quand une URL utilisateur a motive la correction.

## Notes pratiques

- `rg` peut ne pas etre executable dans cet environnement ; utiliser `Get-ChildItem -Recurse -File` et `Select-String` si besoin.
- Le dossier n'est pas forcement un depot Git.
- Les fichiers de sortie CSV et JSON bruts peuvent etre des artefacts de test ; ne pas les supprimer sans demande.
