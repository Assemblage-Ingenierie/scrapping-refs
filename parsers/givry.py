# Parser auto-généré par Gemini pour & GIVRY
# 2026-05-04 12:17
import re

def parse_givry(text: str, url: str, agency: str, image_url: str) -> dict:
    # Extraction des champs simples avec regex
    projet = re.search(r'Projet de .*\n(.*?)\n', text)
    projet = projet.group(1).strip() if projet else ""
    
    mission = re.search(r'Mission : (.*)', text)
    mission = mission.group(1).strip() if mission else ""
    
    surface = re.search(r'Surface : (.*)', text)
    surface = surface.group(1).strip() if surface else ""
    
    # Extraction description (paragraphes après la mission)
    desc_match = re.search(r'Mission : .*\n\n(.*?)(?:\n\n|\nPrécédent)', text, re.DOTALL)
    description = desc_match.group(1).replace('\n', ' ') if desc_match else ""
    
    # Logique métier spécifique
    lieu = url.split('/')[-1] if url else ""
    is_rehab = any(word in text.lower() for word in ['revalorisation', 'réhabilitation', 'rénovation'])
    
    return {
        "agence": agency,
        "projet": projet,
        "maitre_ouvrage": "",
        "mission": mission,
        "programme": "Autre",
        "statut": "Livré",
        "surface_m2": surface,
        "lieu": lieu,
        "annee": None,
        "montant_ht": "",
        "labels": "",
        "bet_equipe": "Éléonore & Givry",
        "rehab_neuf": ["réhabilitation"] if is_rehab else ["neuf"],
        "description": description[:200] + "..." if len(description) > 200 else description,
        "image_url": image_url,
        "url_fiche": url,
        "_selected": True,
        "_is_update": False,
        "_airtable_id": None,
    }
