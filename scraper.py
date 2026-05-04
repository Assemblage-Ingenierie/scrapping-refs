"""
Scraper de références projets pour agences d'architecture.
Usage:
  python scraper.py --agency "Encore Heureux" --url https://encoreheureux.org/fr/projets --max 3
  python scraper.py --config agencies.json --max 6
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import anthropic
import requests
from bs4 import BeautifulSoup

# Charge .env situé dans le même dossier que ce script
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _key, _val = _k.strip(), _v.strip()
                if _val:  # écrase même si déjà présent vide
                    os.environ[_key] = _val

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def fetch_static(url: str, timeout: int = 15) -> str:
    """Fetch URL with requests and return HTML."""
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def fetch_dynamic(url: str) -> str:
    """Fetch JS-rendered page with Playwright (chromium)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(extra_http_headers=HEADERS)
        page.goto(url, wait_until="networkidle", timeout=30_000)
        # Scroll to trigger lazy-loading
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        html = page.content()
        browser.close()
    return html


def is_js_rendered(html: str) -> bool:
    """Heuristic: page is JS-rendered if body has very little visible text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return len(text) < 400


def fetch(url: str, force_dynamic: bool = False) -> tuple[str, bool]:
    """Fetch URL; auto-detect static vs dynamic. Returns (html, used_playwright)."""
    html = fetch_static(url)
    if force_dynamic or is_js_rendered(html):
        print(f"  [playwright] JS-rendu détecté pour {url}")
        html = fetch_dynamic(url)
        return html, True
    return html, False


# ---------------------------------------------------------------------------
# URL extraction helpers (per-agency overrides + generic fallback)
# ---------------------------------------------------------------------------

AGENCY_EXTRACTORS: dict[str, callable] = {}


def register(agency_key: str):
    """Decorator to register a custom URL extractor for an agency."""
    def decorator(fn):
        AGENCY_EXTRACTORS[agency_key.lower()] = fn
        return fn
    return decorator


@register("encore heureux")
def extract_encore_heureux(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    # Project cards are <article> or <a> with /fr/projets/ in href
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        if "/projets/" in full and full != base_url and full not in urls:
            urls.append(full)
    return urls


@register("lt2a")
def extract_lt2a(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        # LT2A project pages: /work/<slug>
        if parsed.path.startswith("/work/") and parsed.path != "/work/" and full not in urls:
            urls.append(full)
    return urls


@register("explorations architecture")
def extract_explorations(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        # WordPress category/projets pages link to individual posts
        if "explorations-architecture.com" in full and "/projets/" in full and full not in urls:
            # Exclude pagination and category pages themselves
            if not re.search(r"/page/\d+", full) and full != base_url:
                urls.append(full)
    return urls


def extract_project_urls_generic(html: str, base_url: str) -> list[str]:
    """Generic heuristic: find anchors that look like project detail pages."""
    soup = BeautifulSoup(html, "html.parser")
    domain = urlparse(base_url).netloc
    seen = set()
    urls = []

    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"])
        parsed = urlparse(full)
        if parsed.netloc != domain:
            continue
        path = parsed.path.rstrip("/")
        # Must have at least 2 segments and not be the listing page itself
        segments = [s for s in path.split("/") if s]
        if len(segments) < 2:
            continue
        if full == base_url or full in seen:
            continue
        # Skip obvious non-project links
        skip_patterns = [
            r"\.(pdf|jpg|jpeg|png|gif|svg|webp|mp4|zip)$",
            r"/(contact|about|team|equipe|agence|actualites|news|blog|mentions-legales|cgv|404)(/|$)",
            r"#",
        ]
        if any(re.search(p, full, re.IGNORECASE) for p in skip_patterns):
            continue
        seen.add(full)
        urls.append(full)

    return urls


def extract_project_urls(agency_name: str, html: str, base_url: str) -> list[str]:
    """Route to agency-specific extractor or fall back to generic."""
    key = agency_name.lower()
    for registered_key, fn in AGENCY_EXTRACTORS.items():
        if registered_key in key or key in registered_key:
            print(f"  [extractor] Utilisation de l'extracteur spécifique: {registered_key}")
            return fn(html, base_url)
    print("  [extractor] Utilisation de l'extracteur générique")
    return extract_project_urls_generic(html, base_url)


# ---------------------------------------------------------------------------
# Project page scraping
# ---------------------------------------------------------------------------

def scrape_project_page(url: str, force_dynamic: bool = False) -> str:
    """Return cleaned HTML of a project detail page."""
    html, _ = fetch(url, force_dynamic=force_dynamic)
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe"]):
        tag.decompose()

    # Try to find main content container
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(id=re.compile(r"(content|main|project)", re.I))
        or soup.find(class_=re.compile(r"(content|main|project|single)", re.I))
        or soup.body
    )

    return main.get_text(separator="\n", strip=True) if main else soup.get_text(separator="\n", strip=True)


def get_first_image(url: str, force_dynamic: bool = False) -> str:
    """Return the first meaningful image URL from a project page."""
    try:
        html, _ = fetch(url, force_dynamic=force_dynamic)
        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if not src or src.startswith("data:"):
                continue
            full = urljoin(url, src)
            if re.search(r"\.(jpg|jpeg|png|webp)", full, re.IGNORECASE):
                return full
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """Tu es un assistant spécialisé dans l'extraction de données de projets d'architecture.
À partir du texte d'une fiche projet, extrait les informations suivantes et réponds UNIQUEMENT avec un JSON valide, sans markdown, sans commentaire.

Champs attendus (null si absent) :
{
  "projet": "nom du projet",
  "maitre_ouvrage": "maître d'ouvrage / client",
  "programme": "type de programme normalisé parmi : Logement, Bureau, Équipement public, Enseignement, Culture, Sport, Santé, Commerce, Industrie, Hôtellerie, Réhabilitation, Espace public, Mixte, Autre",
  "statut": "un parmi : Livré, Chantier, Étude, Concours",
  "surface_m2": nombre entier ou null,
  "lieu": "ville, département ou pays",
  "annee": nombre entier (année de livraison ou de concours) ou null,
  "montant_ht": "montant en euros HT sous forme de texte, ex: 2 500 000 € HT, ou null",
  "labels": "labels séparés par virgule (HQE, BREEAM, BBC, Passivhaus…) ou null",
  "bet_equipe": "bureaux d'études et équipe de conception, ou null",
  "description": "une phrase de description du projet (max 200 caractères)"
}"""


def extract_with_claude(
    client: anthropic.Anthropic,
    agency: str,
    project_url: str,
    text: str,
    image_url: str,
) -> dict:
    """Call Claude to structure project data from raw text."""
    user_msg = f"Agence : {agency}\nURL : {project_url}\n\nTexte de la fiche projet :\n{text[:6000]}"

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  [claude] JSON invalide pour {project_url}: {raw[:200]}")
        data = {}

    data["agence"] = agency
    data["url_fiche"] = project_url
    data["image_url"] = image_url
    return data


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

FIELDS = [
    "agence", "projet", "maitre_ouvrage", "programme", "statut",
    "surface_m2", "lieu", "annee", "montant_ht", "labels",
    "bet_equipe", "description", "image_url", "url_fiche",
]


def save_csv(rows: list[dict], output_path: str):
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})
    print(f"\n[CSV] {len(rows)} projet(s) sauvegardé(s) → {output_path}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def scrape_agency(
    agency_name: str,
    listing_url: str,
    max_projects: int,
    force_dynamic: bool,
    client: anthropic.Anthropic,
) -> list[dict]:
    print(f"\n{'='*60}")
    print(f"Agence : {agency_name}")
    print(f"URL    : {listing_url}")
    print(f"{'='*60}")

    # 1. Fetch listing page
    html, used_pw = fetch(listing_url, force_dynamic=force_dynamic)
    print(f"  [fetch] {'playwright' if used_pw else 'requests'} — {len(html)} octets")

    # 2. Extract project URLs
    urls = extract_project_urls(agency_name, html, listing_url)
    print(f"  [urls] {len(urls)} URL(s) trouvée(s)")
    if not urls:
        print("  [WARN] Aucune URL trouvée — vérifiez l'extracteur.")
        return []

    urls = urls[:max_projects]
    print(f"  [urls] Limité à {len(urls)} projet(s)")

    results = []
    for i, url in enumerate(urls, 1):
        print(f"\n  [{i}/{len(urls)}] {url}")
        try:
            text = scrape_project_page(url, force_dynamic=used_pw)
            image_url = get_first_image(url, force_dynamic=used_pw)
            data = extract_with_claude(client, agency_name, url, text, image_url)
            results.append(data)
            print(f"    projet  : {data.get('projet', '?')}")
            print(f"    lieu    : {data.get('lieu', '?')}")
            print(f"    statut  : {data.get('statut', '?')}")
            print(f"    surface : {data.get('surface_m2', '?')} m²")
        except Exception as e:
            print(f"    [ERREUR] {e}")
        time.sleep(1)  # polite delay

    return results


def main():
    parser = argparse.ArgumentParser(description="Scraper de références projets architecture")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="URL de la page de listing projets")
    group.add_argument("--config", help="Fichier JSON avec liste d'agences")
    parser.add_argument("--agency", help="Nom de l'agence (requis avec --url)")
    parser.add_argument("--max", type=int, default=3, help="Nombre max de projets par agence")
    parser.add_argument("--output", default="projets.csv", help="Fichier CSV de sortie")
    parser.add_argument("--dynamic", action="store_true", help="Forcer Playwright pour toutes les pages")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Erreur : ANTHROPIC_API_KEY non définie.\nCréez un fichier .env avec : ANTHROPIC_API_KEY=sk-ant-...")

    client = anthropic.Anthropic(api_key=api_key)

    agencies = []
    if args.url:
        if not args.agency:
            sys.exit("--agency est requis avec --url")
        agencies = [{"name": args.agency, "url": args.url}]
    else:
        with open(args.config, encoding="utf-8") as f:
            agencies = json.load(f)

    all_results = []
    for ag in agencies:
        rows = scrape_agency(
            agency_name=ag["name"],
            listing_url=ag["url"],
            max_projects=args.max,
            force_dynamic=args.dynamic or ag.get("dynamic", False),
            client=client,
        )
        all_results.extend(rows)

    if all_results:
        save_csv(all_results, args.output)
        # Pretty-print summary
        print("\n--- Résumé ---")
        for r in all_results:
            print(f"  {r.get('agence','?')} | {r.get('projet','?')} | {r.get('lieu','?')} | {r.get('annee','?')}")
    else:
        print("\n[WARN] Aucun résultat extrait.")


if __name__ == "__main__":
    main()
