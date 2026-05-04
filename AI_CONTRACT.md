# AI Contract

Ce contrat fixe les regles strictes pour toute intervention IA sur ce projet.

## Regles non negociables

1. Ne jamais exposer, copier, resumer ou versionner les secrets contenus dans `.env`.
2. Ne jamais supprimer la validation humaine avant l'upload Airtable.
3. Ne jamais casser la deduplication par `url_fiche`.
4. Ne jamais remplacer les parsers rule-based par un appel LLM obligatoire.
5. Ne jamais proposer Claude comme dependance active pour ce projet sans demande explicite du proprietaire.
6. Ne jamais modifier le schema Airtable sans documenter l'impact dans `ARCHITECTURE.md`.
7. Ne jamais envoyer vers Airtable des champs prives frontend commencant par `_`, sauf logique interne explicitement attendue.
8. Ne jamais supprimer des artefacts de test (`*.csv`, `raw_*.json`) sans demande explicite.
9. Ne jamais rendre Gemini obligatoire pour scraper une agence si un chemin deterministe fonctionne.

## Usage IA autorise

L'IA peut aider a :

- comprendre la structure HTML d'une agence ;
- ecrire un extracteur d'URLs dedie ;
- ecrire ou ameliorer un parser regex ;
- proposer des heuristiques de normalisation ;
- generer un parser sauvegarde dans `parsers/` via le fallback Gemini existant ;
- documenter les limites d'extraction et les champs incertains.

L'IA ne doit pas :

- halluciner des donnees absentes de la fiche projet ;
- remplir un maitre d'ouvrage, une surface, un montant ou une annee sans indice dans le texte source ;
- masquer une incertitude sous une valeur plausible ;
- contourner les limites des sites sources ;
- lancer un scraping massif sans limite explicite.

## Schema de sortie projet

Tout parser ou fallback d'extraction doit retourner les champs suivants :

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
- `_selected`
- `_is_update`
- `_airtable_id`

Valeurs attendues :

- `programme` doit appartenir autant que possible a : `Logement`, `Bureau`, `Équipement public`, `Enseignement`, `Culture`, `Sport`, `Santé`, `Commerce`, `Industrie`, `Hôtellerie`, `Réhabilitation`, `Espace public`, `Mixte`, `Autre`.
- `rehab_neuf` doit etre une liste contenant `neuf`, `réhabilitation`, ou les deux.
- `statut` doit appartenir a : `Livré`, `Chantier`, `Étude`, `Concours`.
- `surface_m2` et `annee` doivent etre numeriques ou `None`.
- `description` doit rester courte, idealement sous 200 caracteres.
- `url_fiche` doit etre l'URL source canonique de la fiche.

## Politique LLM

Le projet privilegie l'extraction deterministe :

1. source CMS detectee, par exemple Prismic ;
2. mapping valide par l'utilisateur ;
3. parser specifique existant ;
4. parser generique rule-based ;
5. fallback Gemini optionnel si configure ;
6. generation d'un parser reutilisable pour eviter de refaire appel au LLM.

Claude ne doit pas etre utilise comme chemin de production par defaut. Le fichier `scraper.py` peut contenir un ancien flux Claude, mais il ne represente pas la direction active du backend.

Gemini peut etre utilise pour :

- aider a choisir des URLs projet parmi des liens candidats ;
- proposer un mapping source -> Airtable ;
- extraire une fiche en fallback ;
- generer un parser reutilisable.

Le toggle LLM autorise Gemini, il ne doit pas forcer Gemini sur tous les projets.

Les compteurs d'usage Gemini affiches par l'app sont indicatifs et locaux a la session serveur. Ils ne remplacent pas le tableau de quotas Google.

## Airtable

Avant tout upload :

- l'utilisateur doit avoir vu et valide les lignes ;
- les doublons doivent etre detectes par `url_fiche` ;
- les updates doivent utiliser `_airtable_id` ;
- les creations doivent etre reservees aux fiches sans correspondance.

Le champ `agence` est une organisation liee. Le backend peut creer une organisation manquante via `airtable_find_or_create_org()`.
Le champ `maitre_ouvrage` doit rester un texte simple cote Airtable, sauf changement explicite du schema.

## Scraping responsable

- Limiter le nombre de projets avec `max_projects`.
- Respecter les delais existants entre fiches.
- Utiliser Playwright uniquement si necessaire ou demande.
- Garder un User-Agent explicite.
- Ne pas ajouter de contournement anti-bot agressif.

## Documentation

Toute modification structurelle doit mettre a jour au moins un de ces fichiers :

- `README.md` pour l'usage ;
- `ARCHITECTURE.md` pour le decoupage ;
- `AGENTS.md` pour les consignes de travail ;
- `AI_CONTRACT.md` pour les invariants IA/metier.

## Mapping Humain

Toute proposition de mapping entre un champ source de site et un champ Airtable doit rester corrigeable par l'utilisateur avant scraping. Un champ ambigu doit pouvoir etre mis sur `Ignorer`.
