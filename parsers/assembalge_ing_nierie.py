# Parser auto-généré par Gemini pour Assembalge ingénierie
# 2026-04-30 14:16
import re

def parse_assembalge_ing_nierie(text: str, url: str, agency: str, image_url: str) -> dict:
    def extract(pattern):
        match = re.search(pattern, text)
        return match.group(1).strip() if match else None

    projet = extract(r"Projet\s*:\s*(.*)")
    maitre_ouvrage = extract(r"(?:Maître d'ouvrage|MOA)\s*:\s*(.*)")
    mission = extract(r"Mission\s*:\s*(.*)")
    programme = extract(r"Programme\s*:\s*(.*)")
    statut = extract(r"Statut\s*:\s*(.*)")
    surface = extract(r"Surface\s*:\s*([\d\s]+m2)")
    lieu = extract(r"Lieu\s*:\s*(.*)")
    annee = extract(r"Année\s*:\s*(\d{4})")
    montant = extract(r"Montant\s*:\s*(.*)")
    labels = extract(r"Label\s*:\s*(.*)")
    bet = extract(r"BET\s*:\s*(.*)")
    
    rehab_neuf = []
    if re.search(r"réhabilitation", text, re.IGNORECASE): rehab_neuf.append("réhabilitation")
    if re.search(r"neuf", text, re.IGNORECASE): rehab_neuf.append("neuf")
    
    description_match = re.search(r"Description\s*:\s*(.*(?:\n.*)*)", text, re.IGNORECASE)
    description = description_match.group(1).strip() if description_match else ""

    return {
        "agence": agency,
        "projet": projet or "",
        "maitre_ouvrage": maitre_ouvrage or "",
        "mission": mission or "",
        "programme": programme or "Autre",
        "statut": statut or "",
        "surface_m2": surface or "",
        "lieu": lieu or "",
        "annee": annee,
        "montant_ht": montant or "",
        "labels": labels or "",
        "bet_equipe": bet or "",
        "rehab_neuf": rehab_neuf,
        "description": description,
        "image_url": image_url,
        "url_fiche": url,
        "_selected": True,
        "_is_update": False,
        "_airtable_id": None,
    }
