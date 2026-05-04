"""
Scraping app — FastAPI backend
Run: uvicorn server:app --reload --port 8000
"""

import asyncio
import json
import os
import re
import time
import uuid
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse, quote

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ[k.strip()] = v.strip()

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appgFy9XACtzz4v2P")
AIRTABLE_TABLE   = os.environ.get("AIRTABLE_TABLE", "Référence Projet")
ORG_TABLE        = "Organisation"
AGENCY_PROJECT_URL_FIELD = "URL page projet"
CURRENT_YEAR     = datetime.now().year

GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL     = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")
PARSERS_DIR      = Path(__file__).parent / "parsers"
PARSERS_DIR.mkdir(exist_ok=True)

# The Codex/Windows environment can expose HTTP(S)_PROXY=127.0.0.1:9.
# That intentionally dead proxy breaks Airtable, Gemini and scraping calls.
HTTP = requests.Session()
HTTP.trust_env = False

PRISMIC_DOC_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}
PRISMIC_DOC_BY_URL: dict[str, dict[str, Any]] = {}
GEMINI_USAGE = {
    "date": datetime.now().strftime("%Y-%m-%d"),
    "attempts": 0,
    "success": 0,
    "errors": 0,
    "last_status": "",
    "last_error": "",
    "last_called_at": "",
    "daily_limit": 500,
    "rpm_limit": 5,
    "tpm_limit": 250_000,
}

# ---------------------------------------------------------------------------
# FastAPI setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Scraping Références Archi")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Airtable utilities
# ---------------------------------------------------------------------------
PRIVATE_FIELDS = {"_selected", "_is_update", "_airtable_id", "_error", "_confidence", "_field_sources", "_diagnostics"}
MULTISELECT_FIELDS = {"programme", "labels", "bet_equipe", "rehab_neuf"}
INT_FIELDS = {"surface_m2", "annee"}
LINK_FIELDS = {"agence"}  # → Organisation table
AIRTABLE_FIELD_NAMES = {
    # Airtable field created by the user; keep a Python-safe key in the app.
    "rehab_neuf": "rehab/neuf",
}

AT_HEADERS = lambda: {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    "Content-Type": "application/json",
}


def _at_url(table: str = AIRTABLE_TABLE, path: str = "") -> str:
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(table)}{path}"


# Cache: name → org record_id
_org_cache: dict[str, str] = {}


def airtable_find_or_create_org(name: str) -> str | None:
    """Return Organisation record_id; create if not found."""
    if not name or not name.strip():
        return None
    name = name.strip()
    if name in _org_cache:
        return _org_cache[name]
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    formula = f"LOWER({{Nom}})=LOWER('{safe}')"
    resp = HTTP.get(
        _at_url(ORG_TABLE),
        params={"filterByFormula": formula, "maxRecords": 1},
        headers=AT_HEADERS(), timeout=15,
    )
    resp.raise_for_status()
    recs = resp.json().get("records", [])
    if recs:
        rid = recs[0]["id"]
    else:
        resp = HTTP.post(
            _at_url(ORG_TABLE), headers=AT_HEADERS(),
            json={"records": [{"fields": {"Nom": name}}], "typecast": True},
            timeout=15,
        )
        resp.raise_for_status()
        rid = resp.json()["records"][0]["id"]
    _org_cache[name] = rid
    return rid


def airtable_fetch_all() -> list[dict]:
    """Return all references with id, url_fiche, projet, agence (resolved name)."""
    # Build org id → name map for resolving agence
    org_map = {}
    offset = None
    while True:
        params = {"pageSize": 100}
        if offset: params["offset"] = offset
        resp = HTTP.get(_at_url(ORG_TABLE), headers=AT_HEADERS(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for rec in data.get("records", []):
            org_map[rec["id"]] = rec.get("fields", {}).get("Nom", "")
        offset = data.get("offset")
        if not offset: break

    records, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset: params["offset"] = offset
        resp = HTTP.get(_at_url(), headers=AT_HEADERS(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for rec in data.get("records", []):
            f = rec.get("fields", {})
            agence_ids = f.get("agence", [])
            agence_name = org_map.get(agence_ids[0], "") if agence_ids else ""
            records.append({
                "id":        rec["id"],
                "url_fiche": f.get("url_fiche", ""),
                "projet":    f.get("projet", ""),
                "agence":    agence_name,
            })
        offset = data.get("offset")
        if not offset: break
    return records


def _airtable_text_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, list):
        value = value[0] if value else ""
    return _clean_text(value)


def _parse_montant(raw: str) -> int | None:
    """Parse scraped amounts to a plain integer for Airtable number fields."""
    if raw in (None, ""):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(round(raw))

    s = unicodedata.normalize("NFKC", str(raw))
    s = s.replace("\xa0", " ").replace("\u202f", " ")
    s = re.sub(r"\b(?:ht|ttc|honoraires?|budget|cout|coût|montant|travaux)\b", " ", s, flags=re.I)
    s = s.replace("€", " € ")
    multiplier = 1
    if re.search(r"\b(?:m|mio|million|millions)\b", s, re.I):
        multiplier = 1_000_000
    elif re.search(r"\b(?:k|kilo|millier|milliers)\b", s, re.I):
        multiplier = 1_000

    number_match = re.search(r"\d+(?:[\s.]\d{3})*(?:[,.]\d+)?|\d+(?:[,.]\d+)?", s)
    if not number_match:
        return None
    number = number_match.group(0).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return int(round(float(number) * multiplier))
    except ValueError:
        return None


def _to_airtable_fields(project: dict) -> dict:
    fields: dict[str, Any] = {}

    # Plain text / single select fields
    for k in ("projet", "mission", "lieu", "description", "image_url", "url_fiche", "statut", "maitre_ouvrage"):
        v = project.get(k)
        if v not in (None, ""):
            fields[k] = v

    # Integers
    for k in INT_FIELDS:
        v = project.get(k)
        if v in (None, ""):
            continue
        try:
            fields[k] = int(float(str(v).replace(" ", "").replace("\xa0", "")))
        except (ValueError, TypeError):
            pass

    # Currency: montant_ht
    montant_num = _parse_montant(project.get("montant_ht", ""))
    if montant_num is not None:
        fields["montant_ht"] = montant_num

    # Multi-selects: split by comma
    for k in MULTISELECT_FIELDS:
        v = project.get(k)
        if not v:
            continue
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
        else:
            items = [x.strip() for x in str(v).split(",") if x.strip()]
        if items:
            fields[AIRTABLE_FIELD_NAMES.get(k, k)] = items

    # Linked records
    for k in LINK_FIELDS:
        name = project.get(k)
        if name:
            org_id = airtable_find_or_create_org(name)
            if org_id:
                fields[k] = [org_id]

    return fields


def airtable_create(project: dict) -> dict:
    fields = _to_airtable_fields(project)
    resp = HTTP.post(
        _at_url(), headers=AT_HEADERS(),
        json={"records": [{"fields": fields}], "typecast": True},
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
    rec = resp.json()["records"][0]
    return {"action": "created", "id": rec["id"]}


def airtable_update(record_id: str, project: dict) -> dict:
    fields = _to_airtable_fields(project)
    resp = HTTP.patch(
        _at_url(path=f"/{record_id}"), headers=AT_HEADERS(),
        json={"fields": fields, "typecast": True},
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
    return {"action": "updated", "id": record_id}


# ---------------------------------------------------------------------------
# Scraping utilities
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def fetch_static(url: str) -> str:
    r = HTTP.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def fetch_dynamic(url: str) -> str:
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(extra_http_headers=HEADERS)
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            html = page.content()
            browser.close()
        return html
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "BrowserType.launch" in str(e):
            raise RuntimeError(
                "Playwright Chromium n'est pas installé. Lancez : python -m playwright install chromium"
            ) from e
        raise


def is_js_rendered(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return len(soup.get_text(strip=True)) < 400


def smart_fetch(url: str, force_dynamic: bool = False) -> tuple[str, bool]:
    html = fetch_static(url)
    if force_dynamic or is_js_rendered(html):
        try:
            return fetch_dynamic(url), True
        except Exception:
            return html, False
    return html, False


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\xa0", " ").replace("\u202f", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip(" \n\t:;-")


def _canonical_url(url: str, base_url: str = "") -> str:
    full = urljoin(base_url, url)
    parsed = urlparse(full)
    path = re.sub(r"/{2,}", "/", parsed.path)
    path = path.rstrip("/") or "/"
    netloc = _normalized_netloc(parsed)
    return urlunparse((parsed.scheme, netloc, path, "", "", ""))


def _normalized_netloc(parsed: Any) -> str:
    host = (parsed.hostname or parsed.netloc or "").lower()
    port = parsed.port
    if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
        return f"{host}:{port}"
    return host


def _same_site(url: str, base: str) -> bool:
    return _normalized_netloc(urlparse(url)) == _normalized_netloc(urlparse(base))


def _same_listing_section(url: str, base: str) -> bool:
    base_parts = [part for part in urlparse(base).path.strip("/").split("/") if part]
    url_parts = [part for part in urlparse(url).path.strip("/").split("/") if part]
    if len(base_parts) >= 2 and base_parts[0] == "index.php" and len(url_parts) >= 2 and url_parts[0] == "index.php":
        section = base_parts[1]
        return url_parts[1] in {section, "projets"}
    return True


def resolve_listing_url(agency: str, url: str) -> str:
    """Hook kept for future listing normalization; currently no agency-specific rules."""
    return url


def _push_example(values: list[str], value: Any, max_items: int = 3):
    cleaned = _clean_text(value)
    if cleaned and cleaned not in values and len(values) < max_items:
        values.append(cleaned)


def get_first_image(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
        )
        if not src and img.get("srcset"):
            src = img["srcset"].split(",")[0].strip().split(" ")[0]
        if not src or src.startswith("data:"):
            continue
        full = urljoin(base_url, src)
        alt_class = " ".join(filter(None, [img.get("alt", ""), " ".join(img.get("class", []))])).lower()
        if re.search(r"(logo|icon|avatar|placeholder|sprite)", alt_class):
            continue
        if re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", full, re.IGNORECASE):
            width = int(img.get("width") or 0) if str(img.get("width") or "").isdigit() else 0
            height = int(img.get("height") or 0) if str(img.get("height") or "").isdigit() else 0
            score = width * height
            if img.find_parent(["main", "article"]):
                score += 1_000_000
            candidates.append((score, full))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return ""


def extract_project_urls(agency: str, html: str, base: str) -> list[str]:
    # Generic fallback
    soup = BeautifulSoup(html, "html.parser")
    listing_path = urlparse(base).path.rstrip("/")
    seen, scored = set(), []
    skip = re.compile(
        r"\.(pdf|jpg|jpeg|png|gif|svg|webp|mp4|zip)$"
        r"|/(contact|about|team|equipe|agence|actualites|news|blog|journal|presse|press|"
        r"mentions|legal|privacy|cgv|jobs|carrieres|recrutement|tag|category|author|404|"
        r"infos|interieurs|espace-public)(/|$)",
        re.IGNORECASE,
    )
    for a in soup.find_all("a", href=True):
        raw_href = a["href"].strip()
        if not raw_href or raw_href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = _canonical_url(raw_href, base)
        parsed = urlparse(full)
        if not _same_site(full, base) or not _same_listing_section(full, base) or full in seen or parsed.path.rstrip("/") == listing_path:
            continue
        segs = [s for s in parsed.path.rstrip("/").split("/") if s]
        has_image = bool(a.find("img") or (a.parent and a.parent.find("img")))
        if not segs or skip.search(full):
            continue
        text = _clean_text(a.get_text(" ", strip=True)).lower()
        parent_text = _clean_text(a.parent.get_text(" ", strip=True) if a.parent else "").lower()
        is_exhibit = bool(a.find_parent(class_=re.compile(r"\bexhibit_title\b", re.I)))
        score = 0
        if has_image:
            score += 4
        if is_exhibit:
            score += 4
        if re.search(r"(projet|project|work|realisation|réalisation|reference|référence)", full, re.I):
            score += 3
        if re.search(r"(voir|view|discover|decouvrir|découvrir|projet|project)", text):
            score += 2
        if 1 <= len(segs) <= 4:
            score += 2
        if len(segs) == 1 and has_image:
            score += 2
        if len(parent_text) > 80 and (has_image or is_exhibit):
            score += 1
        if re.search(r"(page|archive|categorie|catégorie|category|tag|news|actualit)", full, re.I):
            score -= 4
        if score < 2:
            continue
        seen.add(full)
        scored.append((score, len(segs), full))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [u for _, _, u in scored]


def detect_prismic_repo(html: str) -> str:
    patterns = [
        r"prismic\.js\?[^\"']*\brepo=([A-Za-z0-9_-]+)",
        r"\bendpoint\s*:\s*[\"']([A-Za-z0-9_-]+)[\"']",
        r"\bapiEndpoint\s*:\s*[\"']https?://([A-Za-z0-9_-]+)\.cdn\.prismic\.io",
        r"https?://([A-Za-z0-9_-]+)\.cdn\.prismic\.io/api/v2",
    ]
    for pattern in patterns:
        m = re.search(pattern, html or "", re.I)
        if m:
            return m.group(1)
    return ""


def fetch_prismic_documents(repo: str, doc_type: str = "projet") -> list[dict[str, Any]]:
    repo = _clean_text(repo)
    if not repo:
        return []
    key = (repo, doc_type)
    if key in PRISMIC_DOC_CACHE:
        return PRISMIC_DOC_CACHE[key]

    api_url = f"https://{repo}.cdn.prismic.io/api/v2"
    api_resp = HTTP.get(api_url, headers=HEADERS, timeout=15)
    api_resp.raise_for_status()
    api_data = api_resp.json()
    refs = api_data.get("refs", [])
    master = next((ref for ref in refs if ref.get("isMasterRef")), refs[0] if refs else None)
    if not master or not master.get("ref"):
        return []

    docs: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = HTTP.get(
            f"{api_url}/documents/search",
            headers=HEADERS,
            params={
                "ref": master["ref"],
                "q": f'[[at(document.type,"{doc_type}")]]',
                "pageSize": 100,
                "page": page,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        docs.extend(data.get("results", []))
        total_pages = int(data.get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1

    PRISMIC_DOC_CACHE[key] = docs
    return docs


def _cms_route_base(listing_url: str, doc_type: str = "projet") -> str:
    parts = [part for part in urlparse(listing_url).path.split("/") if part]
    if parts:
        first = parts[0].strip("/")
        if re.search(r"(projet|project|realisation|r[eé]f[eé]rence)", first, re.I):
            return f"/{first}"
    return "/projets" if doc_type == "projet" else f"/{doc_type}s"


def prismic_project_urls(html: str, listing_url: str) -> list[str]:
    repo = detect_prismic_repo(html)
    if not repo:
        return []
    try:
        docs = fetch_prismic_documents(repo, "projet")
    except Exception:
        return []
    parsed = urlparse(listing_url)
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    route_base = _cms_route_base(listing_url, "projet").rstrip("/")
    urls: list[str] = []
    for doc in docs:
        uid = _clean_text(doc.get("uid"))
        if not uid:
            continue
        full = _canonical_url(f"{origin}{route_base}/{quote(uid.strip('/'), safe='-_~')}")
        PRISMIC_DOC_BY_URL[full] = doc
        urls.append(full)
    return urls


def get_prismic_doc_for_url(project_url: str, html: str = "", listing_url: str = "") -> dict[str, Any] | None:
    canonical = _canonical_url(project_url, listing_url)
    if canonical in PRISMIC_DOC_BY_URL:
        return PRISMIC_DOC_BY_URL[canonical]

    repo = detect_prismic_repo(html)
    if not repo:
        try:
            repo = detect_prismic_repo(fetch_static(project_url))
        except Exception:
            repo = ""
    if not repo:
        return None

    uid = _clean_text(urlparse(project_url).path.rstrip("/").split("/")[-1])
    if not uid:
        return None
    try:
        docs = fetch_prismic_documents(repo, "projet")
    except Exception:
        return None
    for doc in docs:
        if _clean_text(doc.get("uid")) == uid:
            PRISMIC_DOC_BY_URL[canonical] = doc
            return doc
    return None


def extract_link_candidates(html: str, base: str, limit: int = 80) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    candidates: list[dict[str, Any]] = []
    for a in soup.find_all("a", href=True):
        raw_href = a["href"].strip()
        if not raw_href or raw_href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = _canonical_url(raw_href, base)
        parsed = urlparse(full)
        if not _same_site(full, base) or not _same_listing_section(full, base) or full in seen:
            continue
        seen.add(full)
        text = _clean_text(a.get_text(" ", strip=True))
        context = _clean_text(a.parent.get_text(" ", strip=True) if a.parent else "")
        segs = [s for s in parsed.path.rstrip("/").split("/") if s]
        candidates.append({
            "url": full,
            "text": text[:120],
            "context": context[:200],
            "has_image": bool(a.find("img") or (a.parent and a.parent.find("img"))),
            "path_depth": len(segs),
        })
        if len(candidates) >= limit:
            break
    return candidates


def llm_detect_project_urls(agency: str, listing_url: str, candidates: list[dict[str, Any]]) -> list[str]:
    if not candidates:
        return []
    prompt = f"""Tu aides a identifier des fiches projet d'une agence d'architecture.

Agence : {agency}
Page listing : {listing_url}

Voici une liste de liens internes candidats extraits de la page listing.
Selectionne UNIQUEMENT les URLs qui pointent tres probablement vers des fiches projet detaillees, pas vers des pages d'agence, de contact, d'actualites, de categories, de tags, d'archives ou de navigation.

Reponds UNIQUEMENT avec un JSON valide de la forme :
{{"project_urls":["https://...","https://..."]}}

Liens candidats :
{json.dumps(candidates[:80], ensure_ascii=False, indent=2)}
"""
    raw = call_gemini(prompt)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    data = json.loads(raw)
    urls = []
    for value in data.get("project_urls", []):
        full = _canonical_url(str(value), listing_url)
        if full.startswith("http"):
            urls.append(full)
    return urls


def detect_project_urls(agency: str, html: str, base: str, use_llm: bool = False) -> tuple[list[str], str]:
    heuristic_urls = extract_project_urls(agency, html, base)
    prismic_urls = prismic_project_urls(html, base)
    method = "heuristic"
    if prismic_urls:
        merged: list[str] = []
        seen = set()
        for url in heuristic_urls + prismic_urls:
            canonical = _canonical_url(url, base)
            if canonical not in seen:
                seen.add(canonical)
                merged.append(canonical)
        heuristic_urls = merged
        method = "heuristic+prismic" if len(merged) > len(prismic_urls) else "prismic"

    if not use_llm or not GEMINI_API_KEY:
        return heuristic_urls, method

    candidates = extract_link_candidates(html, base)
    if not candidates:
        return heuristic_urls, method

    try:
        llm_urls = llm_detect_project_urls(agency, base, candidates)
    except Exception:
        llm_urls = []

    if not llm_urls:
        return heuristic_urls, method

    merged: list[str] = []
    seen = set()
    prioritized = llm_urls + heuristic_urls if len(heuristic_urls) < 3 else heuristic_urls + llm_urls
    for url in prioritized:
        canonical = _canonical_url(url, base)
        if canonical not in seen:
            seen.add(canonical)
            merged.append(canonical)
    if merged != heuristic_urls:
        method = "heuristic+llm" if heuristic_urls else "llm"
    return merged, method


# ---------------------------------------------------------------------------
# Text parser (rule-based, no Claude needed)
# ---------------------------------------------------------------------------
PROGRAMME_MAP = {
    "logement": "Logement",
    "bureau": "Bureau",
    "bureaux": "Bureau",
    "culture": "Culture",
    "éducation": "Enseignement",
    "école": "Enseignement",
    "ecole": "Enseignement",
    "scolaire": "Enseignement",
    "enseignement": "Enseignement",
    "sport": "Sport",
    "santé": "Santé",
    "commerce": "Commerce",
    "industrie": "Industrie",
    "hôtellerie": "Hôtellerie",
    "espace public": "Espace public",
    "équipement public": "Équipement public",
    "tiers-lieu": "Mixte",
    "réhabilitation": "Réhabilitation",
}
PROGRAMME_VALUES = {
    "Logement", "Bureau", "Équipement public", "Enseignement", "Culture",
    "Sport", "Santé", "Commerce", "Industrie", "Hôtellerie",
    "Réhabilitation", "Espace public", "Mixte", "Autre",
    # Mojibake compatibility for existing source strings in this file.
    "Ã‰quipement public", "SantÃ©", "HÃ´tellerie", "RÃ©habilitation",
}
STATUT_VALUES = {"Livré", "Chantier", "Étude", "Concours", "LivrÃ©", "Ã‰tude"}


LABEL_ALIASES = {
    "maitre_ouvrage": [
        "maître d'ouvrage", "maitrise d'ouvrage", "maîtrise d'ouvrage",
        "maitre d'ouvrage", "commanditaire", "client", "clients", "moa", "ouvrage",
    ],
    "lieu": ["adresse", "lieu", "localisation"],
    "programme": ["programme", "typologie", "type"],
    "mission": ["mission", "missions", "mission confiée", "mission confiee"],
    "livraison": ["livraison", "année", "annee", "date", "date de livraison", "phase"],
    "surface": ["surface", "shon", "sdp", "sub", "surface utile", "surface plancher"],
    "montant_ht": ["montant", "montant des travaux", "budget", "coût", "cout", "travaux"],
    "labels": ["labels", "label", "certification", "certifications", "prix", "récompense", "distinctions"],
    "bet_equipe": [
        "équipe", "equipe", "bet", "intervenants", "maîtrise d'œuvre",
        "maitrise d'oeuvre", "maître d'œuvre", "maitre d'oeuvre", "architecte", "partenaires",
        "consultants", "consultant", "moe", "m.o.e",
    ],
    "rehab_neuf": ["rehab/neuf", "réhab/neuf", "rehabilitation neuf", "neuf rehabilitation", "nature"],
}
 
 
SCRAPPABLE_FIELDS = [
    "projet",
    "maitre_ouvrage",
    "mission",
    "programme",
    "rehab_neuf",
    "statut",
    "surface_m2",
    "lieu",
    "annee",
    "montant_ht",
    "labels",
    "bet_equipe",
    "description",
]

SYNTHETIC_MAPPING_SOURCES = [
    {"value": "__ignore__", "label": "Ignorer"},
    {"value": "__auto__", "label": "Auto"},
    {"value": "__title__", "label": "Titre de la page"},
    {"value": "__description__", "label": "Texte descriptif"},
]


def _ascii_fold(value: str) -> str:
    value = value.lower().replace("œ", "oe")
    return "".join(
        ch for ch in unicodedata.normalize("NFD", value)
        if unicodedata.category(ch) != "Mn"
    )


def _label_key(label: str) -> str | None:
    norm = _ascii_fold(_clean_text(label))
    for key, aliases in LABEL_ALIASES.items():
        for alias in aliases:
            alias_norm = _ascii_fold(alias)
            if norm == alias_norm:
                return key
            if len(alias_norm) > 4 and re.search(rf"(?<!\w){re.escape(alias_norm)}(?!\w)", norm):
                return key
    return None


def _first_nonempty(*values: Any) -> str:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return ""


def _merge_meta_value(meta: dict[str, str], key: str, value: str):
    value = _clean_text(value)
    if not value:
        return
    if key in meta and meta[key]:
        if value not in meta[key]:
            meta[key] = f"{meta[key]}, {value}"
    else:
        meta[key] = value


def _mapping_confidence(label: str, suggested: str) -> float:
    if suggested == "__ignore__":
        return 0.2
    folded = _ascii_fold(label)
    key = _label_key(label)
    if key == suggested:
        return 0.95
    if suggested == "surface_m2" and any(x in folded for x in ("surface", "sdp", "shon", "sub")):
        return 0.9
    if suggested == "annee" and any(x in folded for x in ("annee", "livraison", "date")):
        return 0.8
    return 0.45 if suggested else 0.2


def _target_from_label(label: str) -> str:
    folded = _ascii_fold(label)
    if "uvre" in folded or "oeuvre" in folded:
        return "bet_equipe"
    key = _label_key(label)
    if key == "surface":
        return "surface_m2"
    if key == "livraison":
        return "annee"
    return key or "__ignore__"


def _find_inline_label_before_colon(text: str, colon_index: int) -> tuple[int, str] | None:
    """Find the shortest known metadata label immediately before a colon."""
    prefix = text[:colon_index].rstrip()
    window_start = max(0, len(prefix) - 90)
    window = prefix[window_start:]
    tokens = list(re.finditer(r"\S+", window))
    for token in reversed(tokens):
        candidate = window[token.start():].strip(" -|/\\")
        folded_candidate = _ascii_fold(candidate)
        if folded_candidate.startswith(("d'", "de ", "du ", "des ")):
            continue
        if len(candidate) > 80:
            continue
        if _label_key(candidate):
            return window_start + token.start(), candidate
    return None


def _split_inline_metadata(text: str) -> list[tuple[str, str]]:
    """Split flattened metadata like 'Surface: 10 m2 Budget: 1 M EUR'."""
    text = _clean_text(text)
    if ":" not in text:
        return []

    labels: list[tuple[int, int, str]] = []
    for colon in re.finditer(":", text):
        found = _find_inline_label_before_colon(text, colon.start())
        if not found:
            continue
        label_start, label = found
        if labels and label_start < labels[-1][1]:
            continue
        labels.append((label_start, colon.end(), label))

    pairs: list[tuple[str, str]] = []
    for index, (_label_start, value_start, label) in enumerate(labels):
        value_end = labels[index + 1][0] if index + 1 < len(labels) else len(text)
        value = _clean_text(text[value_start:value_end])
        if value:
            pairs.append((label, value))
    return pairs


PRISMIC_TARGET_MAP = {
    "titre_projet": "projet",
    "maitre_ouvrage": "maitre_ouvrage",
    "mission": "mission",
    "programme": "programme",
    "annee_projet": "annee",
    "calendrier": "annee",
    "surface_cout": "surface_m2",
    "certification": "labels",
    "equipe": "bet_equipe",
    "description_du_projet": "description",
    "texte": "description",
    "description": "description",
    "lieu": "lieu",
    "localisation": "lieu",
}


def _prismic_value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str | int | float):
        return _clean_text(value)
    if isinstance(value, list):
        parts = [_prismic_value_to_text(item) for item in value]
        return _clean_text(" ".join(part for part in parts if part))
    if isinstance(value, dict):
        if value.get("text"):
            return _clean_text(value.get("text"))
        if value.get("url"):
            return _clean_text(value.get("url"))
        parts = []
        for key in ("alt", "copyright", "name"):
            if value.get(key):
                parts.append(str(value[key]))
        return _clean_text(" ".join(parts))
    return _clean_text(value)


def prismic_doc_to_raw_samples(doc: dict[str, Any], url: str) -> dict[str, dict]:
    samples: dict[str, dict] = {}
    for label, raw_value in (doc.get("data") or {}).items():
        value = _prismic_value_to_text(raw_value)
        if not value or len(value) > 900:
            continue
        target = PRISMIC_TARGET_MAP.get(label, _target_from_label(label))
        confidence = 0.95 if label in PRISMIC_TARGET_MAP else _mapping_confidence(label, target)
        samples[label] = {
            "source_label": label,
            "sample_values": [value[:260]],
            "urls": [url],
            "suggested_field": target,
            "confidence": confidence,
        }
    if not any(sample.get("suggested_field") == "lieu" for sample in samples.values()):
        raw = doc.get("data") or {}
        title = _prismic_value_to_text(raw.get("titre_projet")) or _clean_text(doc.get("uid"))
        inferred = infer_location(title=title, url=url, text=_prismic_value_to_text(raw.get("description_du_projet")))
        if inferred:
            samples["lieu_inferé"] = {
                "source_label": "lieu_inferé",
                "sample_values": [inferred],
                "urls": [url],
                "suggested_field": "lieu",
                "confidence": 0.75,
            }
    return samples


def _meta_key_for_target(target: str) -> str:
    return {
        "surface_m2": "surface",
        "annee": "livraison",
    }.get(target, target)


def _is_target_oriented_mapping(mapping: dict[str, str] | None) -> bool:
    if not mapping:
        return False
    return any(key in SCRAPPABLE_FIELDS for key in mapping.keys())


def _add_raw_meta(samples: dict[str, dict], label: str, value: str, url: str):
    label = _clean_text(label)
    value = _clean_text(value)
    value = re.split(
        r"\s+(?:CLIENT|CONSULTANTS?|SURFACE|PHASE|PROGRAMME|MISSION|MA[ÎI]TRE D[’' ]OUVRAGE)\s*[:：]\s*",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    inline_pairs = _split_inline_metadata(f"{label}: {value}")
    if len(inline_pairs) > 1 and _label_key(inline_pairs[0][0]) == _label_key(label):
        value = inline_pairs[0][1]
    if not label or not value or len(label) > 90 or len(value) > 260:
        return
    folded_label = _ascii_fold(label)
    if (
        "posted by" in folded_label
        or folded_label in {"http", "https", "image"}
        or len(label.split()) > 6
        or re.search(r"\b\d{1,2}\s+\w+\s+20\d{2}\b", folded_label)
        or folded_label.count("/") >= 2
    ):
        return
    entry = samples.setdefault(label, {
        "source_label": label,
        "sample_values": [],
        "urls": [],
        "suggested_field": _target_from_label(label),
        "confidence": 0,
    })
    if value not in entry["sample_values"]:
        entry["sample_values"].append(value)
    if url not in entry["urls"]:
        entry["urls"].append(url)
    entry["confidence"] = max(entry["confidence"], _mapping_confidence(label, entry["suggested_field"]))


def _add_raw_meta_pairs(samples: dict[str, dict], text: str, url: str) -> bool:
    pairs = _split_inline_metadata(text)
    if not pairs:
        return False
    for label, value in pairs:
        _add_raw_meta(samples, label, value, url)
    return True


def extract_raw_metadata_samples(html: str, url: str) -> dict[str, dict]:
    """Return raw label/value pairs for human mapping review."""
    soup = BeautifulSoup(html, "html.parser")
    samples: dict[str, dict] = {}

    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            _add_raw_meta(samples, dt.get_text(" ", strip=True), dd.get_text(" ", strip=True), url)

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            _add_raw_meta(samples, cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True), url)

    for node in soup.find_all(["li", "p", "div", "span"]):
        if node.find(["p", "li", "dl", "table", "div"]):
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        if not text or len(text) > 300:
            continue
        if _add_raw_meta_pairs(samples, text, url):
            continue
        m = re.match(r"^([^:：]+)\s*[:：]\s*(.+)$", text)
        if m:
            _add_raw_meta(samples, m.group(1), m.group(2), url)

    lines = [_clean_text(line) for line in soup.get_text("\n", strip=True).split("\n")]
    lines = [line for line in lines if line]
    for i, line in enumerate(lines[:-1]):
        if _add_raw_meta_pairs(samples, line, url):
            continue
        inline = re.match(r"^([^:：]{2,80})\s*[:：]\s*(.+)$", line)
        if inline and _label_key(inline.group(1)):
            _add_raw_meta(samples, inline.group(1), inline.group(2), url)
            continue
        label = line.rstrip(":")
        if ":" in label or "：" in label:
            continue
        if not _label_key(label):
            continue
        j = i + 1
        if j < len(lines) and lines[j] in {":", "|"}:
            j += 1
        if j < len(lines):
            _add_raw_meta(samples, label, lines[j], url)

    return samples


def apply_field_mapping(meta: dict[str, str], raw_samples: dict[str, dict], mapping: dict[str, str] | None):
    if not mapping:
        return
    if _is_target_oriented_mapping(mapping):
        for target, source in mapping.items():
            if target not in SCRAPPABLE_FIELDS or not source or source.startswith("__"):
                continue
            sample = raw_samples.get(source)
            if not sample:
                continue
            values = sample.get("sample_values", [])
            if values:
                _merge_meta_value(meta, _meta_key_for_target(target), values[0])
        return

    for label, target in mapping.items():
        if not target or target == "__ignore__":
            continue
        sample = raw_samples.get(label)
        if not sample:
            continue
        values = sample.get("sample_values", [])
        if values:
            _merge_meta_value(meta, _meta_key_for_target(target), values[0])


def _description_from_text(text: str, title: str = "") -> str:
    for paragraph in re.split(r"\n{2,}", text or ""):
        value = _clean_text(paragraph)
        if not value or value == title:
            continue
        if len(value) >= 80:
            return value[:900]
    lines = [_clean_text(line) for line in (text or "").splitlines() if _clean_text(line)]
    if len(lines) >= 2:
        return " ".join(lines[1:6])[:900]
    return ""


def apply_project_field_mapping(project: dict, payload: dict, mapping: dict[str, str] | None):
    raw_samples = payload.get("raw_meta", {})
    title = payload.get("title", "")
    text = payload.get("text", "")
    if not _is_target_oriented_mapping(mapping):
        for source, target in (mapping or {}).items():
            if target not in SCRAPPABLE_FIELDS or source in {"__ignore__", "__auto__"}:
                continue
            sample = raw_samples.get(source)
            values = sample.get("sample_values", []) if sample else []
            if values:
                project[target] = values[0]
        return

    for target, source in (mapping or {}).items():
        if target not in SCRAPPABLE_FIELDS or not source or source in {"__ignore__", "__auto__"}:
            continue
        value = ""
        if source == "__title__":
            value = title
        elif source == "__description__":
            value = _description_from_text(text, title)
        else:
            sample = raw_samples.get(source)
            if sample:
                values = sample.get("sample_values", [])
                value = values[0] if values else ""
        if value:
            project[target] = value


def _default_source_for_target(target: str) -> tuple[str, float]:
    defaults = {
        "projet": ("__title__", 0.8),
        "programme": ("__auto__", 0.45),
        "rehab_neuf": ("__auto__", 0.55),
        "statut": ("__auto__", 0.45),
        "lieu": ("__auto__", 0.35),
        "description": ("__description__", 0.55),
    }
    return defaults.get(target, ("__ignore__", 0.0))


def build_target_mapping_rows(fields: list[dict], parsed_examples: dict[str, list[str]] | None = None) -> list[dict]:
    by_target: dict[str, dict] = {}
    for field in fields:
        target = field.get("suggested_field")
        if target in SCRAPPABLE_FIELDS and target not in by_target:
            by_target[target] = field

    rows = []
    for target in SCRAPPABLE_FIELDS:
        detected = by_target.get(target)
        if detected:
            rows.append({
                "target_field": target,
                "suggested_source": detected["source_label"],
                "sample_values": detected.get("sample_values", []),
                "target_examples": (parsed_examples or {}).get(target, []),
                "confidence": detected.get("confidence", 0.0),
            })
            continue

        source, confidence = _default_source_for_target(target)
        rows.append({
            "target_field": target,
            "suggested_source": source,
            "sample_values": [],
            "target_examples": (parsed_examples or {}).get(target, []),
            "confidence": confidence,
        })
    return rows


def build_source_options(fields: list[dict]) -> list[dict]:
    options = [dict(option) for option in SYNTHETIC_MAPPING_SOURCES]
    seen = {option["value"] for option in options}
    for field in fields:
        label = field.get("source_label")
        if label and label not in seen:
            options.append({"value": label, "label": label})
            seen.add(label)
    return options


def build_source_samples(fields: list[dict], synthetic_samples: dict[str, list[str]] | None = None) -> dict[str, dict]:
    samples: dict[str, dict] = {
        "__ignore__": {"sample_values": [], "example": "Ignorer"},
        "__auto__": {"sample_values": [], "example": "Détection automatique"},
        "__title__": {"sample_values": [], "example": "Titre de la page"},
        "__description__": {"sample_values": [], "example": "Texte descriptif"},
    }
    for field in fields:
        samples[field["source_label"]] = {
            "sample_values": list(field.get("sample_values", [])),
            "example": (field.get("sample_values", []) or [""])[0],
        }
    for source, values in (synthetic_samples or {}).items():
        entry = samples.setdefault(source, {"sample_values": [], "example": ""})
        for value in values:
            _push_example(entry["sample_values"], value)
        if entry["sample_values"]:
            entry["example"] = entry["sample_values"][0]
    return samples


def extract_structured_metadata(soup: BeautifulSoup) -> dict[str, str]:
    """Extract label/value metadata from common project page structures."""
    meta: dict[str, str] = {}

    for dt in soup.find_all("dt"):
        key = _label_key(dt.get_text(" ", strip=True))
        dd = dt.find_next_sibling("dd")
        if key and dd:
            _merge_meta_value(meta, key, dd.get_text(" ", strip=True))

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            key = _label_key(cells[0].get_text(" ", strip=True))
            if key:
                _merge_meta_value(meta, key, cells[1].get_text(" ", strip=True))

    for node in soup.find_all(["li", "p", "div", "span"]):
        if node.find(["p", "li", "dl", "table", "div"]):
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        if not text or len(text) > 260:
            continue
        m = re.match(r"^([^:：]+)\s*[:：]\s*(.+)$", text)
        if not m:
            continue
        key = _label_key(m.group(1))
        if key:
            _merge_meta_value(meta, key, m.group(2))

    for label_node in soup.find_all(string=True):
        raw_label = _ascii_fold(_clean_text(str(label_node)))
        exact_labels = {
            _ascii_fold(alias)
            for aliases in LABEL_ALIASES.values()
            for alias in aliases
        }
        if raw_label not in exact_labels:
            continue
        key = _label_key(str(label_node))
        parent = label_node.parent
        if not key or not parent:
            continue
        if parent.name in {"dt", "th"}:
            continue
        sibling = parent.find_next_sibling()
        if sibling:
            value = sibling.get_text(" ", strip=True)
            if value and len(value) < 220 and ":" not in value:
                _merge_meta_value(meta, key, value)

    return meta


def extract_title(soup: BeautifulSoup, fallback_url: str) -> str:
    for selector in ("h1", "article h2", "main h2", "[class*=title]", "[class*=titre]"):
        node = soup.select_one(selector)
        if node:
            title = _clean_text(node.get_text(" ", strip=True))
            if 3 < len(title) < 140:
                return title
    og = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
    if og and og.get("content"):
        return _clean_text(og["content"].split("|")[0].split(" - ")[0])
    if soup.title and soup.title.string:
        return _clean_text(soup.title.string.split("|")[0].split(" - ")[0])
    return _clean_text(urlparse(fallback_url).path.rstrip("/").split("/")[-1].replace("-", " "))


def extract_page_payload(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    meta = extract_structured_metadata(soup)
    raw_meta = extract_raw_metadata_samples(html, url)
    image_url = get_first_image(html, url)
    title = extract_title(soup, url)
    if not any(sample.get("suggested_field") == "lieu" for sample in raw_meta.values()):
        inferred = infer_location(title=title, url=url, text=soup.get_text(" ", strip=True))
        if inferred:
            raw_meta["lieu_inferé"] = {
                "source_label": "lieu_inferé",
                "sample_values": [inferred],
                "urls": [url],
                "suggested_field": "lieu",
                "confidence": 0.75,
            }

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "iframe", "form"]):
        tag.decompose()
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(id=re.compile(r"(content|main|project|projet)", re.I))
        or soup.find(class_=re.compile(r"(content|main|project|projet|single)", re.I))
        or soup.body
    )
    text = main.get_text(separator="\n", strip=True) if main else ""
    return {"text": text, "meta": meta, "raw_meta": raw_meta, "image_url": image_url, "title": title}


def normalize_programme(raw: str) -> str:
    if not raw:
        return "Autre"
    # Multiple programmes = Mixte
    if re.search(r"[,/]", raw):
        return "Mixte"
    lower = raw.lower()
    folded = _ascii_fold(raw)
    for k, v in PROGRAMME_MAP.items():
        if k in lower or _ascii_fold(k) in folded:
            return v
    return raw.strip().capitalize() or "Autre"


def normalize_statut(raw: str) -> str:
    folded = _ascii_fold(raw or "")
    if "concours" in folded:
        return "Concours"
    if "chantier" in folded or "cours" in folded:
        return "Chantier"
    if "livre" in folded or "livraison" in folded:
        return "Livré"
    if "etude" in folded:
        return "Étude"
    return "Étude"


def parse_surface(raw: str) -> int | None:
    raw = _clean_text(raw)
    if not raw:
        return None
    m = re.search(r"(\d[\d\s.,]*)\s*(?:m2|m²|sqm|m\s²)", raw, re.I)
    if not m:
        m = re.search(r"(\d[\d\s.,]*)", raw)
    if not m:
        return None
    number = m.group(1).replace("\u202f", " ").replace("\xa0", " ").replace(" ", "")
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    if 1 <= value <= 10_000_000:
        return int(round(value))
    return None


def extract_year(raw: str) -> int | None:
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", raw or "")]
    if not years:
        return None
    plausible = [y for y in years if 1950 <= y <= CURRENT_YEAR + 10]
    return plausible[-1] if plausible else years[-1]


LOCATION_STOPWORDS = {
    "projet", "projets", "project", "projects", "construction", "rehabilitation", "réhabilitation",
    "extension", "surelevation", "surélévation", "renovation", "rénovation", "maison", "maisons",
    "logement", "logements", "ecole", "école", "groupe", "centre", "pole", "pôle", "immeuble",
    "batiment", "bâtiment", "residence", "résidence", "creation", "création", "concours",
}


def _format_location(value: str) -> str:
    value = _clean_text(value)
    value = re.sub(r"^[\d\s._-]+", "", value)
    value = re.sub(r"\s+", " ", value.replace("_", " "))
    value = value.strip(" -_,.")
    if not value:
        return ""

    def format_piece(piece: str) -> str:
        return "-".join(part[:1].upper() + part[1:].lower() for part in piece.split("-") if part)

    return " ".join(format_piece(part) for part in value.split())


def _looks_like_location(value: str) -> bool:
    folded = _ascii_fold(value)
    if not value or len(value) < 3 or len(value) > 70:
        return False
    if re.search(r"\d", value):
        return False
    words = [w for w in re.split(r"[\s-]+", folded) if w]
    if not words or len(words) > 5:
        return False
    if any(word in LOCATION_STOPWORDS for word in words):
        return False
    return True


def infer_location(title: str = "", url: str = "", text: str = "") -> str:
    candidates: list[str] = []

    for source in (title, ""):
        source = _clean_text(source)
        if not source:
            continue
        for sep in (" - ", " – ", " — ", " | "):
            if sep in source:
                candidates.append(source.split(sep, 1)[0])
                break

    path_slug = urlparse(url).path.rstrip("/").split("/")[-1] if url else ""
    slug = re.sub(r"^\d+[_-]+", "", path_slug)
    slug = re.sub(r"[_-]{2,}", "-", slug)
    if slug:
        tokens = [token for token in re.split(r"[-_]+", slug) if token]
        kept = []
        for token in tokens:
            folded = _ascii_fold(token)
            if folded.isdigit() or folded in LOCATION_STOPWORDS:
                break
            kept.append(token)
            if len(kept) >= 4:
                break
        if kept:
            candidates.append("-".join(kept))

    m = re.search(r"\b(?:à|a|au|aux|en)\s+([A-ZÉÈÀÂÎÔÛÇ][\wÉÈÀÂÎÔÛÇéèàâîôûç' -]{2,45})", text or "")
    if m:
        candidates.append(m.group(1))

    for candidate in candidates:
        formatted = _format_location(candidate)
        if _looks_like_location(formatted):
            return formatted
    return ""


def extract_montant(raw: str) -> str:
    raw = _clean_text(raw)
    if not raw:
        return ""
    m = re.search(
        r"(\d[\d\s.,]*\s*(?:M|m|K|k)?\s*(?:€|EUR|euros?)(?:\s*H\.?T\.?)?)",
        raw,
        re.I,
    )
    return _clean_text(m.group(1)) if m else ""


def infer_rehab_neuf(*parts: str) -> list[str]:
    text = _ascii_fold(" ".join(_clean_text(p) for p in parts if p))
    rehab_patterns = [
        r"\brehab", r"\brehabilitation\b", r"\brenovation\b", r"\brestructuration\b",
        r"\breconversion\b", r"\btransformation\b", r"\bextension\b",
        r"\bsurelevation\b", r"\bexistant\b", r"\bpatrimoine\b", r"\brestauration\b",
    ]
    neuf_patterns = [
        r"\bneuf\b", r"\bneuve\b", r"\bconstruction neuve\b", r"\bnouvelle construction\b",
        r"\bbatiment neuf\b", r"\bcreat(?:ion|e)\b", r"\bconstruction d",
    ]
    values = []
    if any(re.search(p, text) for p in neuf_patterns):
        values.append("neuf")
    if any(re.search(p, text) for p in rehab_patterns):
        values.append("réhabilitation")
    return values or ["neuf"]


def project_confidence(project: dict) -> dict[str, Any]:
    required = ["projet", "programme", "statut", "lieu", "url_fiche"]
    useful = ["maitre_ouvrage", "mission", "surface_m2", "annee", "montant_ht", "description", "image_url", "rehab_neuf"]
    present_required = sum(1 for key in required if project.get(key))
    present_useful = sum(1 for key in useful if project.get(key))
    score = round((present_required / len(required)) * 0.65 + (present_useful / len(useful)) * 0.35, 2)
    missing = [key for key in required + useful if not project.get(key)]
    return {"score": score, "missing": missing}


def finalize_project(project: dict, text: str, meta: dict[str, str] | None = None) -> dict:
    meta = meta or {}
    project["programme"] = normalize_programme(project.get("programme", ""))
    if project["programme"] not in PROGRAMME_VALUES:
        project["programme"] = "Autre"
    project["statut"] = normalize_statut(project.get("statut", ""))
    if not project.get("lieu"):
        project["lieu"] = infer_location(
            title=project.get("projet", ""),
            url=project.get("url_fiche", ""),
            text=text[:2500],
        )

    if not project.get("rehab_neuf"):
        project["rehab_neuf"] = infer_rehab_neuf(
            meta.get("rehab_neuf", ""),
            project.get("programme", ""),
            project.get("description", ""),
            text[:2500],
        )
    elif isinstance(project["rehab_neuf"], str):
        project["rehab_neuf"] = infer_rehab_neuf(project["rehab_neuf"])
    else:
        normalized = []
        for value in project["rehab_neuf"]:
            folded = _ascii_fold(str(value))
            if "neuf" in folded or "neuve" in folded:
                normalized.append("neuf")
            if "rehab" in folded or "renov" in folded:
                normalized.append("réhabilitation")
        project["rehab_neuf"] = list(dict.fromkeys(normalized)) or infer_rehab_neuf(text[:2500])

    confidence = project_confidence(project)
    project["_confidence"] = confidence["score"]
    project["_diagnostics"] = {
        "missing_fields": confidence["missing"],
        "meta_keys": sorted(meta.keys()),
        "text_length": len(text or ""),
    }
    return project


def prismic_doc_to_project(doc: dict[str, Any], url: str, agency: str) -> tuple[dict, dict]:
    raw = doc.get("data") or {}
    samples = prismic_doc_to_raw_samples(doc, url)
    meta: dict[str, str] = {}
    for label, sample in samples.items():
        target = sample.get("suggested_field")
        values = sample.get("sample_values", [])
        if target in SCRAPPABLE_FIELDS and values:
            _merge_meta_value(meta, _meta_key_for_target(target), values[0])

    title = _prismic_value_to_text(raw.get("titre_projet")) or _clean_text(doc.get("uid"))
    description = _prismic_value_to_text(raw.get("description_du_projet"))
    programme = _prismic_value_to_text(raw.get("programme"))
    calendrier = _prismic_value_to_text(raw.get("calendrier")) or _prismic_value_to_text(raw.get("annee_projet"))
    surface_cout = _prismic_value_to_text(raw.get("surface_cout"))
    full_text = "\n".join(
        part for part in [
            title,
            programme,
            _prismic_value_to_text(raw.get("maitre_ouvrage")),
            _prismic_value_to_text(raw.get("mission")),
            _prismic_value_to_text(raw.get("equipe")),
            surface_cout,
            calendrier,
            _prismic_value_to_text(raw.get("certification")),
            description,
        ]
        if part
    )
    image_url = _prismic_value_to_text(raw.get("image_entete")) or _prismic_value_to_text(raw.get("image_couverture"))
    project = {
        "projet": title,
        "agence": agency,
        "maitre_ouvrage": _prismic_value_to_text(raw.get("maitre_ouvrage")),
        "mission": _prismic_value_to_text(raw.get("mission")),
        "programme": programme,
        "rehab_neuf": infer_rehab_neuf(programme, description, full_text),
        "statut": infer_statut(full_text, calendrier),
        "surface_m2": parse_surface(surface_cout),
        "lieu": (
            _prismic_value_to_text(raw.get("lieu"))
            or _prismic_value_to_text(raw.get("localisation"))
            or infer_location(title=title, url=url, text=description)
        ),
        "annee": extract_year(calendrier),
        "montant_ht": extract_montant(surface_cout),
        "labels": _prismic_value_to_text(raw.get("certification")),
        "bet_equipe": _prismic_value_to_text(raw.get("equipe")),
        "description": description,
        "image_url": image_url,
        "url_fiche": url,
    }
    payload = {
        "text": full_text,
        "meta": meta,
        "raw_meta": samples,
        "image_url": image_url,
        "title": title,
    }
    return project, payload


def infer_statut(text: str, livraison: str) -> str:
    tl = text.lower()
    if "non lauréat" in tl or "non-lauréat" in tl:
        return "Concours"
    if "concours" in tl and "lauréat" not in tl:
        return "Concours"
    if re.search(r"en chantier", tl):
        return "Chantier"
    if livraison:
        year = extract_year(livraison)
        if year is not None:
            if year < CURRENT_YEAR:
                return "Livré"
            if year <= CURRENT_YEAR + 2:
                return "Chantier"
            return "Étude"
    return "Étude"


def _get_field(text: str, *labels: str, meta: dict[str, str] | None = None) -> str:
    if meta:
        for label in labels:
            key = _label_key(label)
            if key and meta.get(key):
                return meta[key]
    for label in labels:
        m = re.search(rf"(?:^|\n)\s*{re.escape(label)}\s*(?:\n|[:：|])\s*([^\n]+)", text, re.I)
        if m:
            value = _clean_text(m.group(1))
            if value not in {":", "|"}:
                return value
    lines = [_clean_text(line) for line in text.split("\n") if _clean_text(line)]
    wanted = {_ascii_fold(label) for label in labels}
    for i, line in enumerate(lines[:-1]):
        if _ascii_fold(line.rstrip(":")) in wanted:
            j = i + 1
            if lines[j] in {":", "|"}:
                j += 1
            if j < len(lines):
                return lines[j]
    return ""


def parse_project_text(
    text: str,
    url: str,
    agency: str,
    image_url: str,
    meta: dict[str, str] | None = None,
    title: str = "",
) -> dict:
    meta = meta or {}
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    projet = _clean_text(title) or (lines[0] if lines else "")
    # Skip boilerplate first lines
    boilerplate = {
        "afficher", "galerie + description", "galerie", "vignettes", "description",
        "menu", "accueil", "projets", "projects", "work", "works",
    }
    for line in lines:
        clean_line = _clean_text(line)
        if len(clean_line) > 3 and clean_line.lower() not in boilerplate:
            if not projet or clean_line.lower() in projet.lower():
                projet = clean_line
            break

    maitre_ouvrage = _get_field(text, "Commanditaire", "Maître d'ouvrage", "Maitre d'ouvrage", "Client", "CLIENT", "Maîtrise d'ouvrage", meta=meta)
    mission        = _get_field(text, "Mission", "Missions", "Mission confiée", "Mission confiee", meta=meta)
    lieu           = _get_field(text, "Adresse", "Lieu", "Localisation", "Ville", meta=meta)
    if not lieu:
        lieu = infer_location(title=projet, url=url, text=text[:2500])
    programme_raw  = _get_field(text, "Programme", "Typologie", "Type", meta=meta)
    livraison      = _get_field(text, "Livraison", "Année", "Annee", "Date de livraison", "PHASE", "Phase", meta=meta)
    equipe         = _get_field(text, "Équipe", "Equipe", "BET", "Intervenants", "Maîtrise d'œuvre", "CONSULTANTS", "Consultants", meta=meta)

    # Surface
    surface_raw = _get_field(text, "Surface", "SHON", "SDP", "SUB", meta=meta)
    surface_m2 = parse_surface(surface_raw)
    if surface_m2 is None:
        surface_m2 = parse_surface(text)

    # Year / statut
    annee = extract_year(livraison) or extract_year(text)
    statut = infer_statut(text, livraison)

    # Montant HT
    montant_ht = extract_montant(meta.get("montant_ht", "")) or extract_montant(text)

    # Labels + Prix
    labels = _get_field(text, "Labels", "Label", "Certification", "Certifications", meta=meta)
    prix   = _get_field(text, "Prix", "Récompense", "Recompense", "Distinctions", meta=meta)
    labels_combined = ", ".join(filter(None, [_clean_text(labels), _clean_text(prix)]))
    rehab_neuf = infer_rehab_neuf(meta.get("rehab_neuf", ""), programme_raw, labels_combined, text[:2500])

    # Description: paragraph after title / "Description" heading, before first labeled section
    desc = ""
    desc_m = re.search(
        r"Description\n(.+?)(?=\nAdresse|\nCommanditaire|\nSurface|\nLabels|"
        r"\nLivraison|\nProgramme|\nMontant|\nÉquipe|\nEquipe|\nBET|\nPrix|\nGalerie|\nVoir aussi|\Z)",
        text, re.DOTALL | re.I,
    )
    if desc_m:
        raw_desc = _clean_text(desc_m.group(1))
        sentences = re.split(r"(?<=[.!?])\s+", raw_desc)
        desc = " ".join(sentences[:2])[:200]
    if not desc:
        candidates = [
            _clean_text(line) for line in lines
            if 80 <= len(_clean_text(line)) <= 320
            and not _label_key(_clean_text(line).split(":")[0])
        ]
        if candidates:
            desc = candidates[0][:200]

    return {
        "agence":         agency,
        "projet":         projet,
        "maitre_ouvrage": maitre_ouvrage,
        "mission":        mission,
        "programme":      normalize_programme(programme_raw),
        "statut":         statut,
        "surface_m2":     surface_m2,
        "lieu":           lieu,
        "annee":          annee,
        "montant_ht":     montant_ht,
        "labels":         labels_combined,
        "bet_equipe":     equipe,
        "rehab_neuf":     rehab_neuf,
        "description":    desc,
        "image_url":      image_url,
        "url_fiche":      url,
        # frontend-only
        "_selected":      True,
        "_is_update":     False,
        "_airtable_id":   None,
    }

    r"""
    for line in lines:
        if len(line) > 3 and line not in ("Afficher", "Galerie + Description", "Galerie", "Vignettes", "Description"):
            projet = line
            break

    maitre_ouvrage = _get_field(text, "Commanditaire", "Maître d'ouvrage", "Client", "Maîtrise d'ouvrage")
    lieu           = _get_field(text, "Adresse", "Lieu", "Localisation", "Ville")
    programme_raw  = _get_field(text, "Programme")
    livraison      = _get_field(text, "Livraison", "Année", "Date de livraison")
    equipe         = _get_field(text, "Équipe", "BET", "Intervenants")

    # Surface
    surface_m2 = None
    surface_raw = _get_field(text, "Surface", "SHON", "SDP", "SUB")
    if surface_raw:
        nums = re.sub(r"[\s \xa0]", "", surface_raw)
        m = re.search(r"(\d+)", nums)
        if m:
            try:
                surface_m2 = int(m.group(1))
            except ValueError:
                pass

    # Year / statut
    annee = None
    if livraison:
        m = re.search(r"(\d{4})", livraison)
        if m:
            annee = int(m.group(1))
    statut = infer_statut(text, livraison)

    # Montant HT
    montant_ht = ""
    m = re.search(r"\nMontant(?: des travaux)?\n([\d\s \xa0]+€)\n?(HT)?", text)
    if m:
        montant_ht = m.group(1).strip() + (" HT" if m.group(2) else "")
    if not montant_ht:
        m = re.search(r"([\d\s \xa0]+(?:M)?€[\s\xa0]?HT)", text)
        if m:
            montant_ht = m.group(1).strip()

    # Labels + Prix
    labels = _get_field(text, "Labels", "Label")
    prix   = _get_field(text, "Prix", "Récompense", "Distinctions")
    labels_combined = ", ".join(filter(None, [labels, prix]))

    # Description: paragraph after title / "Description" heading, before first labeled section
    desc = ""
    desc_m = re.search(
        r"Description\n(.+?)(?=\nAdresse|\nCommanditaire|\nSurface|\nLabels|"
        r"\nLivraison|\nProgramme|\nMontant|\nÉquipe|\nPrix|\nGalerie|\nVoir aussi|\Z)",
        text, re.DOTALL,
    )
    if desc_m:
        raw_desc = desc_m.group(1).strip()
        # First 2 sentences, max 200 chars
        sentences = re.split(r"(?<=[.!?])\s+", raw_desc)
        desc = " ".join(sentences[:2])[:200]

    return {
        "agence":         agency,
        "projet":         projet,
        "maitre_ouvrage": maitre_ouvrage,
        "programme":      normalize_programme(programme_raw),
        "statut":         statut,
        "surface_m2":     surface_m2,
        "lieu":           lieu,
        "annee":          annee,
        "montant_ht":     montant_ht,
        "labels":         labels_combined,
        "bet_equipe":     equipe,
        "description":    desc,
        "image_url":      image_url,
        "url_fiche":      url,
        # frontend-only
        "_selected":      True,
        "_is_update":     False,
        "_airtable_id":   None,
    }
    """


PARSERS: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# LLM (Gemini) layer
# ---------------------------------------------------------------------------
def _reset_gemini_usage_if_needed():
    today = datetime.now().strftime("%Y-%m-%d")
    if GEMINI_USAGE.get("date") == today:
        return
    GEMINI_USAGE.update({
        "date": today,
        "attempts": 0,
        "success": 0,
        "errors": 0,
        "last_status": "",
        "last_error": "",
        "last_called_at": "",
    })


def _safe_error_text(exc: Exception) -> str:
    text = str(exc)
    if GEMINI_API_KEY:
        text = text.replace(GEMINI_API_KEY, "[REDACTED]")
    return text[:500]


def gemini_usage_snapshot() -> dict[str, Any]:
    _reset_gemini_usage_if_needed()
    remaining = max(0, int(GEMINI_USAGE["daily_limit"]) - int(GEMINI_USAGE["attempts"]))
    return {**GEMINI_USAGE, "remaining_today": remaining}


def call_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY non configurée")
    _reset_gemini_usage_if_needed()
    GEMINI_USAGE["attempts"] += 1
    GEMINI_USAGE["last_called_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    GEMINI_USAGE["last_status"] = "running"
    GEMINI_USAGE["last_error"] = ""
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        resp = HTTP.post(
            endpoint,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        resp.raise_for_status()
        GEMINI_USAGE["success"] += 1
        GEMINI_USAGE["last_status"] = "success"
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        GEMINI_USAGE["errors"] += 1
        GEMINI_USAGE["last_status"] = "error"
        GEMINI_USAGE["last_error"] = _safe_error_text(e)
        raise


def llm_extract_project(text: str, url: str, agency: str, image_url: str) -> dict:
    prompt = f"""Tu es un assistant expert en architecture. Extrais les informations suivantes d'une fiche projet d'architecture et réponds UNIQUEMENT avec un JSON valide, sans markdown ni explication.

Champs attendus :
{{
  "projet": "nom du projet",
  "maitre_ouvrage": "client ou maître d'ouvrage",
  "mission": "mission confiée à l'agence, ex : concours, étude, conception, maîtrise d'œuvre, BASE, EXE",
  "lieu": "ville, département ou région",
  "programme": "un seul parmi : Logement|Bureau|Équipement public|Enseignement|Culture|Sport|Santé|Commerce|Industrie|Hôtellerie|Réhabilitation|Espace public|Mixte|Autre",
  "statut": "un seul parmi : Livré|Chantier|Étude|Concours",
  "surface_m2": null,
  "annee": null,
  "montant_ht": "",
  "labels": "",
  "bet_equipe": "",
  "rehab_neuf": ["neuf"],
  "description": ""
}}

Texte de la fiche (max 3000 chars) :
---
{text[:3000]}
---"""

    raw = call_gemini(prompt)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    data = json.loads(raw)

    return {
        "agence":         agency,
        "projet":         str(data.get("projet") or ""),
        "maitre_ouvrage": str(data.get("maitre_ouvrage") or ""),
        "mission":        str(data.get("mission") or ""),
        "programme":      str(data.get("programme") or "Autre"),
        "statut":         str(data.get("statut") or "Étude"),
        "surface_m2":     data.get("surface_m2") or None,
        "lieu":           str(data.get("lieu") or ""),
        "annee":          data.get("annee") or None,
        "montant_ht":     str(data.get("montant_ht") or ""),
        "labels":         str(data.get("labels") or ""),
        "bet_equipe":     str(data.get("bet_equipe") or ""),
        "rehab_neuf":     data.get("rehab_neuf") or [],
        "description":    str(data.get("description") or "")[:200],
        "image_url":      image_url,
        "url_fiche":      url,
        "_selected":      True,
        "_is_update":     False,
        "_airtable_id":   None,
        "_llm":           True,
    }


def llm_suggest_field_mapping(
    agency: str,
    fields: list[dict],
    parsed_examples: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    if not fields:
        return {}
    prompt = f"""Tu aides a faire le mapping entre des champs detectes sur un site d'agence d'architecture et des champs Airtable cibles.

Agence : {agency}

Champs Airtable autorises :
{json.dumps(SCRAPPABLE_FIELDS, ensure_ascii=False)}

Champs detectes sur le site :
{json.dumps([
    {
        "source_label": field.get("source_label"),
        "sample_values": field.get("sample_values", [])[:3],
        "suggested_field": field.get("suggested_field"),
        "confidence": field.get("confidence"),
    }
    for field in fields
], ensure_ascii=False, indent=2)}

Exemples de valeurs deja parsees automatiquement sur les fiches :
{json.dumps(parsed_examples or {}, ensure_ascii=False, indent=2)}

Reponds UNIQUEMENT avec un JSON valide de la forme :
{{
  "suggestions": [
    {{"source_label": "CLIENT", "target_field": "maitre_ouvrage", "confidence": 0.98}}
  ]
}}

Ne propose que des target_field presents dans la liste autorisee. Si un champ doit etre ignore, utilise "__ignore__".
"""
    raw = call_gemini(prompt)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    data = json.loads(raw)
    suggestions: dict[str, dict[str, Any]] = {}
    for item in data.get("suggestions", []):
        label = _clean_text(item.get("source_label"))
        target = _clean_text(item.get("target_field"))
        if not label or (target not in SCRAPPABLE_FIELDS and target != "__ignore__"):
            continue
        confidence = float(item.get("confidence") or 0.0)
        suggestions[label] = {"target_field": target, "confidence": max(0.0, min(confidence, 1.0))}
    return suggestions


def generate_and_save_parser(agency: str, text_example: str, extracted: dict) -> str | None:
    """Ask Gemini to write a Python parser for this agency and save to parsers/."""
    agency_key = re.sub(r"[^a-z0-9]+", "_", agency.lower()).strip("_")
    parser_path = PARSERS_DIR / f"{agency_key}.py"
    if parser_path.exists():
        return None  # Already generated

    ext_json = json.dumps(
        {k: v for k, v in extracted.items() if not k.startswith("_")},
        ensure_ascii=False, indent=2,
    )

    prompt = f"""Tu es un expert Python. Génère une fonction Python qui extrait des champs de fiches projets de l'agence d'architecture "{agency}" à partir du texte brut de la page.

Exemple de texte réel d'une fiche :
---
{text_example[:2000]}
---

Les données correctement extraites de ce texte sont :
{ext_json}

Génère uniquement la fonction Python suivante, sans imports ni code extérieur :

def parse_{agency_key}(text: str, url: str, agency: str, image_url: str) -> dict:
    # utilise uniquement re (déjà importé dans le fichier hôte)
    ...
    return {{
        "agence": agency, "projet": ..., "maitre_ouvrage": ..., "mission": ...,
        "programme": ..., "statut": ..., "surface_m2": ...,
        "lieu": ..., "annee": ..., "montant_ht": ...,
        "labels": ..., "bet_equipe": ..., "rehab_neuf": ..., "description": ...,
        "image_url": image_url, "url_fiche": url,
        "_selected": True, "_is_update": False, "_airtable_id": None,
    }}

Réponds UNIQUEMENT avec le code de la fonction, sans aucun autre texte."""

    try:
        code = call_gemini(prompt)
        code = re.sub(r"^```(?:python)?\s*", "", code.strip())
        code = re.sub(r"\s*```$", "", code.strip())
        if not code.strip().startswith("def "):
            return None

        full_code = (
            f"# Parser auto-généré par Gemini pour {agency}\n"
            f"# {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"import re\n\n"
            f"{code}\n"
        )
        parser_path.write_text(full_code, encoding="utf-8")
        return str(parser_path)
    except Exception:
        return None


def load_saved_parsers() -> int:
    """Load all parsers from parsers/ dir into PARSERS dict. Returns count loaded."""
    import importlib.util
    count = 0
    for py_file in sorted(PARSERS_DIR.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(py_file.stem, str(py_file))
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn_name = f"parse_{py_file.stem}"
            if hasattr(mod, fn_name):
                label = py_file.stem.replace("_", " ")
                PARSERS[label] = getattr(mod, fn_name)
                count += 1
        except Exception:
            pass
    return count


load_saved_parsers()


def scrape_project(
    url: str,
    agency: str,
    used_playwright: bool,
    use_llm: bool = False,
    field_mapping: dict[str, str] | None = None,
) -> dict:
    html = fetch_dynamic(url) if used_playwright else fetch_static(url)
    prismic_doc = get_prismic_doc_for_url(url, html, url)
    if prismic_doc:
        result, payload = prismic_doc_to_project(prismic_doc, url, agency)
        apply_project_field_mapping(result, payload, field_mapping)
        result = finalize_project(result, payload["text"], payload["meta"])
        result["_raw_text"] = payload["text"]
        return result

    payload = extract_page_payload(html, url)
    text = payload["text"]
    image_url = payload["image_url"]
    meta = payload["meta"]
    apply_field_mapping(meta, payload.get("raw_meta", {}), field_mapping)
    title = payload["title"]

    # Dispatch to per-agency parser (hardcoded or saved from LLM)
    key = agency.lower()
    for rk, fn in PARSERS.items():
        if rk in key or key in rk:
            try:
                result = fn(text, url, agency, image_url, meta, title)
            except TypeError:
                result = fn(text, url, agency, image_url)
            for field, value in parse_project_text(text, url, agency, image_url, meta, title).items():
                if field.startswith("_"):
                    continue
                if result.get(field) in (None, "") and value not in (None, ""):
                    result[field] = value
            apply_project_field_mapping(result, payload, field_mapping)
            result = finalize_project(result, text, meta)
            result["_raw_text"] = text
            return result

    # LLM fallback for unknown agencies
    if use_llm and GEMINI_API_KEY:
        try:
            result = llm_extract_project(text, url, agency, image_url)
            apply_project_field_mapping(result, payload, field_mapping)
            result = finalize_project(result, text, meta)
            result["_raw_text"] = text
            return result
        except Exception as e:
            pass  # Fall through to regex

    result = parse_project_text(text, url, agency, image_url, meta, title)
    apply_project_field_mapping(result, payload, field_mapping)
    result = finalize_project(result, text, meta)
    result["_raw_text"] = text
    return result


# ---------------------------------------------------------------------------
# Background jobs (in-memory, single-user)
# ---------------------------------------------------------------------------
jobs: dict[str, dict] = {}


def _bg_generate_parser(agency: str, text: str, extracted: dict):
    path = generate_and_save_parser(agency, text, extracted)
    if path:
        load_saved_parsers()


def run_scraping_job(
    job_id: str, agency: str, url: str,
    max_projects: int, dynamic: bool, use_llm: bool = False,
    field_mapping: dict[str, str] | None = None,
    start_index: int = 0,
):
    import threading
    job = jobs[job_id]
    parser_generated = False
    try:
        url = resolve_listing_url(agency, url)
        job["logs"].append("Chargement de la page listing…")
        html, used_pw = smart_fetch(url, force_dynamic=dynamic)
        job["logs"].append(f"Page chargée ({'Playwright' if used_pw else 'requests'})")

        # Check if a saved LLM parser exists for this agency
        key = agency.lower()
        has_parser = any(rk in key or key in rk for rk in PARSERS)
        if use_llm and not has_parser:
            job["logs"].append("Mode hybride : LLM activé pour cette agence inconnue")
        elif use_llm and has_parser:
            job["logs"].append("Parser existant trouvé — LLM non nécessaire")

        all_urls, detection_method = detect_project_urls(agency, html, url, use_llm)
        start_index = max(0, int(start_index or 0))
        urls = all_urls[:max_projects][start_index:]
        job["total"] = len(urls)
        if start_index:
            job["logs"].append(f"Reprise au projet {start_index + 1}")
        job["logs"].append(f"{len(urls)} projet(s) trouvé(s)")
        if detection_method != "heuristic":
            helper = "Gemini" if "llm" in detection_method else "CMS"
            job["logs"].append(f"Détection des URLs assistée par {helper} ({detection_method})")

        if not urls:
            job["status"] = "done"
            return

        for i, project_url in enumerate(urls):
            slug = project_url.rstrip("/").split("/")[-1] or project_url
            job["logs"].append(f"Scraping {i+1}/{len(urls)} — {slug}")
            try:
                project = scrape_project(project_url, agency, used_pw, use_llm, field_mapping)
                raw_text = project.pop("_raw_text", "")

                if project.get("_llm"):
                    job["logs"][-1] += " ✓ [LLM]"
                    if not parser_generated and raw_text:
                        parser_generated = True
                        job["logs"].append("Génération du parser pour prochains scrapings…")
                        t = threading.Thread(
                            target=_bg_generate_parser,
                            args=(agency, raw_text, project),
                            daemon=True,
                        )
                        t.start()
                else:
                    job["logs"][-1] += " ✓"

                job["projects"].append(project)
            except Exception as e:
                job["logs"][-1] += f" ✗ ({e})"
            job["progress"] = i + 1
            time.sleep(0.5)

        job["status"] = "done"
        if parser_generated:
            job["logs"].append("Parser sauvegardé — sera utilisé automatiquement la prochaine fois")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
class ScrapeRequest(BaseModel):
    agency: str
    url: str
    max_projects: int = 50
    dynamic: bool = False
    use_llm: bool = False
    sample_url: str | None = None
    start_index: int = 0
    field_mapping: dict[str, str] | None = None


class UpsertRequest(BaseModel):
    projects: list[dict]


@app.get("/api/agencies")
async def get_agencies():
    """Return all Organisations with Type='Architecte' and their URL page projet."""
    if not AIRTABLE_API_KEY:
        raise HTTPException(400, "AIRTABLE_API_KEY non configurée")

    def fetch():
        records, offset = [], None
        while True:
            params = {
                "pageSize": 100,
                "filterByFormula": "FIND('Architecte',{Compétences})",
            }
            if offset:
                params["offset"] = offset
            resp = HTTP.get(_at_url(ORG_TABLE), headers=AT_HEADERS(), params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get("records", []):
                f = rec.get("fields", {})
                project_url = _airtable_text_value(f.get(AGENCY_PROJECT_URL_FIELD))
                fallback_url = _airtable_text_value(f.get("URL site")) or _airtable_text_value(f.get("Site web"))
                records.append({
                    "id":      rec["id"],
                    "name":    _airtable_text_value(f.get("Nom")),
                    "url":     project_url or fallback_url,
                    "has_url": bool(project_url),
                })
            offset = data.get("offset")
            if not offset:
                break
        records.sort(key=lambda r: r["name"].lower())
        return records

    try:
        return {"agencies": await asyncio.to_thread(fetch)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/airtable/records")
async def get_airtable_records():
    if not AIRTABLE_API_KEY:
        raise HTTPException(400, "AIRTABLE_API_KEY non configurée")
    try:
        records = await asyncio.to_thread(airtable_fetch_all)
        return {"records": records, "count": len(records)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/scrape/start")
async def scrape_start(req: ScrapeRequest):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "total": 0,
        "projects": [],
        "logs": [],
        "error": None,
    }
    asyncio.get_event_loop().run_in_executor(
        None, run_scraping_job,
        job_id, req.agency, req.url, req.max_projects, req.dynamic,
        req.use_llm, req.field_mapping, req.start_index,
    )
    return {"job_id": job_id}


@app.post("/api/scrape/preview")
async def scrape_preview(req: ScrapeRequest):
    """Fetch listing page and preview candidate project URLs without scraping details."""
    try:
        def run():
            listing_url = resolve_listing_url(req.agency, req.url)
            html, used_pw = smart_fetch(listing_url, force_dynamic=req.dynamic)
            urls, detection_method = detect_project_urls(req.agency, html, listing_url, req.use_llm)
            return {
                "urls": urls[:req.max_projects],
                "count": len(urls),
                "used_playwright": used_pw,
                "listing_url": listing_url,
                "detection_method": detection_method,
            }

        return await asyncio.to_thread(run)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/scrape/analyze-fields")
async def scrape_analyze_fields(req: ScrapeRequest):
    """Sample a few project pages and propose field mappings before scraping."""
    try:
        def run():
            listing_url = resolve_listing_url(req.agency, req.url)
            html, used_pw = smart_fetch(listing_url, force_dynamic=req.dynamic)
            all_urls, detection_method = detect_project_urls(req.agency, html, listing_url, req.use_llm)
            if req.sample_url:
                sample_url = _canonical_url(req.sample_url, listing_url)
                urls = [sample_url] if sample_url in {_canonical_url(u, listing_url) for u in all_urls} else []
            else:
                urls = all_urls[: min(req.max_projects, 3)]
            merged: dict[str, dict] = {}
            parsed_examples: dict[str, list[str]] = {}
            synthetic_samples = {"__title__": [], "__description__": []}
            for project_url in urls:
                project_html = fetch_dynamic(project_url) if used_pw else fetch_static(project_url)
                prismic_doc = get_prismic_doc_for_url(project_url, project_html, listing_url)
                if prismic_doc:
                    parsed, payload = prismic_doc_to_project(prismic_doc, project_url, req.agency)
                    samples = payload.get("raw_meta", {})
                    parsed = finalize_project(parsed, payload["text"], payload["meta"])
                else:
                    payload = extract_page_payload(project_html, project_url)
                    samples = extract_raw_metadata_samples(project_html, project_url)
                    parsed = parse_project_text(
                        payload["text"],
                        project_url,
                        req.agency,
                        payload["image_url"],
                        payload["meta"],
                        payload["title"],
                    )
                    parsed = finalize_project(parsed, payload["text"], payload["meta"])
                for label, sample in samples.items():
                    entry = merged.setdefault(label, {
                        "source_label": label,
                        "sample_values": [],
                        "urls": [],
                        "suggested_field": sample["suggested_field"],
                        "confidence": sample["confidence"],
                    })
                    for value in sample["sample_values"]:
                        if value not in entry["sample_values"] and len(entry["sample_values"]) < 3:
                            entry["sample_values"].append(value)
                    for sample_url in sample["urls"]:
                        if sample_url not in entry["urls"]:
                            entry["urls"].append(sample_url)
                    entry["confidence"] = max(entry["confidence"], sample["confidence"])
                _push_example(synthetic_samples["__title__"], payload.get("title", ""))
                _push_example(synthetic_samples["__description__"], _description_from_text(payload.get("text", ""), payload.get("title", "")))
                for target in SCRAPPABLE_FIELDS:
                    value = parsed.get(target)
                    if isinstance(value, list):
                        for item in value:
                            _push_example(parsed_examples.setdefault(target, []), item)
                    else:
                        _push_example(parsed_examples.setdefault(target, []), value)
                time.sleep(0.25)

            fields = sorted(merged.values(), key=lambda x: (-x["confidence"], x["source_label"].lower()))
            if req.use_llm and GEMINI_API_KEY and fields:
                try:
                    suggestions = llm_suggest_field_mapping(req.agency, fields, parsed_examples)
                except Exception:
                    suggestions = {}
                for field in fields:
                    suggestion = suggestions.get(field["source_label"])
                    if not suggestion:
                        continue
                    current = field.get("suggested_field", "__ignore__")
                    current_conf = float(field.get("confidence") or 0.0)
                    if current_conf < 0.9 or current == "__ignore__":
                        field["suggested_field"] = suggestion["target_field"]
                    field["confidence"] = max(current_conf, float(suggestion.get("confidence") or 0.0))

            mapping_rows = build_target_mapping_rows(fields, parsed_examples)
            source_options = build_source_options(fields)
            source_samples = build_source_samples(fields, synthetic_samples)
            return {
                "urls": urls,
                "listing_url": listing_url,
                "used_playwright": used_pw,
                "detection_method": detection_method,
                "fields": fields,
                "mapping_rows": mapping_rows,
                "source_options": source_options,
                "source_samples": source_samples,
                "parsed_examples": parsed_examples,
                "target_fields": ["__ignore__", *SCRAPPABLE_FIELDS],
            }

        return await asyncio.to_thread(run)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/scrape/{job_id}")
async def scrape_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")
    return job


@app.post("/api/upsert")
async def upsert(req: UpsertRequest):
    if not AIRTABLE_API_KEY:
        raise HTTPException(400, "AIRTABLE_API_KEY non configurée")

    results = []
    for project in req.projects:
        url = project.get("url_fiche", "")
        record_id = project.get("_airtable_id")
        try:
            if record_id:
                r = await asyncio.to_thread(airtable_update, record_id, project)
            else:
                r = await asyncio.to_thread(airtable_create, project)
            results.append({"url_fiche": url, "success": True, **r})
        except Exception as e:
            results.append({"url_fiche": url, "success": False, "error": str(e)})
        await asyncio.sleep(0.25)  # Airtable rate limit

    return {"results": results}


def _reload_env():
    """Re-read .env and refresh module-level API key vars."""
    global GEMINI_API_KEY, GEMINI_MODEL
    if _env.exists():
        for line in _env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ[k.strip()] = v.strip()
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")


@app.get("/api/llm/status")
async def llm_status():
    _reload_env()
    parsers_list = [
        {"name": f.stem.replace("_", " "), "file": f.name}
        for f in sorted(PARSERS_DIR.glob("*.py"))
    ]
    return {
        "configured": bool(GEMINI_API_KEY),
        "model": GEMINI_MODEL,
        "usage": gemini_usage_snapshot(),
        "saved_parsers": parsers_list,
    }


@app.get("/api/parsers")
async def list_parsers():
    parsers_list = []
    for f in sorted(PARSERS_DIR.glob("*.py")):
        parsers_list.append({
            "name": f.stem.replace("_", " "),
            "file": f.name,
            "size": f.stat().st_size,
            "created": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return {"parsers": parsers_list}


@app.delete("/api/parsers/{filename}")
async def delete_parser(filename: str):
    safe_name = Path(filename).name
    if not safe_name.endswith(".py"):
        raise HTTPException(400, "Fichier parser invalide")
    path = PARSERS_DIR / safe_name
    if not path.exists():
        raise HTTPException(404, "Parser introuvable")
    try:
        path.unlink()
        PARSERS.pop(path.stem.replace("_", " "), None)
        load_saved_parsers()
        return {"deleted": safe_name}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = static_dir / "index.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Frontend non trouvé — placez index.html dans static/</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
