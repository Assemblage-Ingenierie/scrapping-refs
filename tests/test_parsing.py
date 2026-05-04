import unittest
from unittest.mock import patch

import server


class ParsingTests(unittest.TestCase):
    def test_rehab_neuf_detects_both(self):
        values = server.infer_rehab_neuf(
            "Réhabilitation lourde avec extension neuve d'un bâtiment existant"
        )
        self.assertEqual(values, ["neuf", "réhabilitation"])

    def test_airtable_field_mapping_for_rehab_neuf(self):
        fields = server._to_airtable_fields({
            "projet": "Projet test",
            "mission": "BASE + EXE",
            "rehab_neuf": ["neuf", "réhabilitation"],
        })
        self.assertEqual(fields["mission"], "BASE + EXE")
        self.assertEqual(fields["rehab/neuf"], ["neuf", "réhabilitation"])

    def test_maitre_ouvrage_is_plain_text_in_airtable_payload(self):
        fields = server._to_airtable_fields({
            "agence": "Agence test",
            "maitre_ouvrage": "Ville de Paris",
            "projet": "Projet test",
        })
        self.assertEqual(fields["maitre_ouvrage"], "Ville de Paris")
        self.assertIsInstance(fields["agence"], list)

    def test_airtable_payload_exports_montant_as_integer(self):
        fields = server._to_airtable_fields({
            "projet": "Projet test",
            "montant_ht": "15,5 M€HT",
        })
        self.assertEqual(fields["montant_ht"], 15500000)
        self.assertIsInstance(fields["montant_ht"], int)

    def test_raw_metadata_suggests_mission_mapping(self):
        html = "<main><p>Mission: Concours</p></main>"
        samples = server.extract_raw_metadata_samples(html, "https://example.com/projet")
        self.assertEqual(samples["Mission"]["suggested_field"], "mission")

    def test_inline_metadata_keeps_label_and_value_separate(self):
        html = """
        <main>
          <p>Programme : Bureaux, commerces</p>
          <p>Coût : 50 000 000 €HT</p>
          <p>Surface : 16 000 m²</p>
          <p>Maîtrise d'ouvrage : Icade Promotion tertiaire</p>
          <p>Equipe : Marc Mimram Architecture</p>
        </main>
        """
        samples = server.extract_raw_metadata_samples(html, "https://example.com/projet")
        self.assertEqual(samples["Programme"]["sample_values"], ["Bureaux, commerces"])
        self.assertEqual(samples["Surface"]["sample_values"], ["16 000 m2"])
        self.assertEqual(samples["Maîtrise d'ouvrage"]["sample_values"], ["Icade Promotion tertiaire"])

    def test_flattened_metadata_splits_multiple_label_value_pairs(self):
        html = """
        <main>
          <p>Lieu : Paris 19 Typologie : Tertiaire et formation</p>
          <p>Surface : 4 550 m2 SP Budget : 15 MEURHT MOE : & GIVRY</p>
        </main>
        """
        samples = server.extract_raw_metadata_samples(html, "https://andgivry.com/projet/aubervilliers")
        self.assertEqual(samples["Lieu"]["sample_values"], ["Paris 19"])
        self.assertEqual(samples["Typologie"]["sample_values"], ["Tertiaire et formation"])
        self.assertEqual(samples["Surface"]["sample_values"], ["4 550 m2 SP"])
        self.assertEqual(samples["Budget"]["sample_values"], ["15 MEURHT"])
        self.assertEqual(samples["MOE"]["sample_values"], ["& GIVRY"])
        self.assertEqual(samples["MOE"]["suggested_field"], "bet_equipe")

    def test_extract_year_prefers_latest_delivery_year(self):
        self.assertEqual(
            server.extract_year("Competition 2020 – Completion 2025"),
            2025,
        )

    def test_infer_statut_uses_latest_year_in_range(self):
        future_year = server.CURRENT_YEAR + 4
        statut = server.infer_statut("Projet en cours", f"Competition 2023 – Completion {future_year}")
        self.assertEqual(statut, "Étude")

    def test_parse_project_text_keeps_latest_future_delivery_year(self):
        future_year = server.CURRENT_YEAR + 3
        text = """
        Projet test
        PHASE
        Competition 2021 – Completion FUTURE_YEAR
        """
        text = text.replace("FUTURE_YEAR", str(future_year))
        project = server.parse_project_text(
            text,
            "https://example.com/projet",
            "Agence",
            "",
            {"livraison": f"Competition 2021 – Completion {future_year}"},
            "Projet test",
        )
        self.assertEqual(project["annee"], future_year)

    def test_structured_metadata_feeds_generic_parser(self):
        html = """
        <html><body><main>
          <h1>Maison transformée</h1>
          <dl>
            <dt>Maître d'ouvrage</dt><dd>Ville test</dd>
            <dt>Programme</dt><dd>Réhabilitation, Logement neuf</dd>
            <dt>Surface</dt><dd>1 200 m²</dd>
          </dl>
          <p>Transformation d'un bâtiment existant avec extension neuve.</p>
          <img src="/photo.jpg" width="1000" height="700">
        </main></body></html>
        """
        payload = server.extract_page_payload(html, "https://example.com/projets/test")
        project = server.parse_project_text(
            payload["text"],
            "https://example.com/projets/test",
            "Agence",
            payload["image_url"],
            payload["meta"],
            payload["title"],
        )
        project = server.finalize_project(project, payload["text"], payload["meta"])

        self.assertEqual(project["surface_m2"], 1200)
        self.assertEqual(project["rehab_neuf"], ["neuf", "réhabilitation"])
        self.assertEqual(project["maitre_ouvrage"], "Ville test")

    def test_detect_project_urls_merges_llm_candidates(self):
        with patch.object(server, "GEMINI_API_KEY", "test-key"):
            with patch.object(server, "extract_project_urls", return_value=["https://example.com/projets/alpha"]):
                with patch.object(server, "extract_link_candidates", return_value=[{"url": "https://example.com/beta"}]):
                    with patch.object(server, "llm_detect_project_urls", return_value=["https://example.com/beta"]):
                        urls, method = server.detect_project_urls("Agence", "<main></main>", "https://example.com/projets", use_llm=True)
        self.assertEqual(method, "heuristic+llm")
        self.assertIn("https://example.com/beta", urls)
        self.assertIn("https://example.com/projets/alpha", urls)

    def test_default_https_port_is_same_site_for_url_detection(self):
        html = '<a href="https://aclaa.fr:443/index.php/architecture/test-projet/">Projet test</a>'
        urls = server.extract_link_candidates(html, "https://aclaa.fr/index.php/architecture/")
        self.assertEqual(urls[0]["url"], "https://aclaa.fr/index.php/architecture/test-projet")

    def test_indexhibit_listing_rejects_other_sections(self):
        html = """
        <li class="exhibit_title"><a href="https://aclaa.fr:443/index.php/architecture/test/">Projet</a></li>
        <li class="exhibit_title"><a href="https://aclaa.fr:443/index.php/urbanisme/test/">Urbanisme</a></li>
        """
        urls = server.extract_project_urls("ACLAA", html, "https://aclaa.fr/index.php/architecture/")
        self.assertEqual(urls, ["https://aclaa.fr/index.php/architecture/test"])

    def test_detect_prismic_repo_from_script(self):
        html = '<script src="https://static.cdn.prismic.io/prismic.js?new=true&repo=belval-parquet-architectes"></script>'
        self.assertEqual(server.detect_prismic_repo(html), "belval-parquet-architectes")

    def test_prismic_urls_are_used_when_html_has_no_links(self):
        html = '<script src="https://static.cdn.prismic.io/prismic.js?new=true&repo=demo-repo"></script>'
        docs = [{"uid": "maison-test", "data": {}}]
        with patch.object(server, "fetch_prismic_documents", return_value=docs):
            with patch.object(server, "extract_project_urls", return_value=[]):
                urls, method = server.detect_project_urls("Agence", html, "https://example.com/projets", use_llm=False)
        self.assertEqual(method, "prismic")
        self.assertEqual(urls, ["https://example.com/projets/maison-test"])

    def test_prismic_raw_samples_suggest_airtable_targets(self):
        doc = {
            "uid": "test",
            "data": {
                "surface_cout": "1 250 m2",
                "maitre_ouvrage": [{"type": "paragraph", "text": "Ville test"}],
            },
        }
        samples = server.prismic_doc_to_raw_samples(doc, "https://example.com/projets/test")
        self.assertEqual(samples["surface_cout"]["suggested_field"], "surface_m2")
        self.assertEqual(samples["maitre_ouvrage"]["sample_values"], ["Ville test"])

    def test_infer_location_from_project_title(self):
        self.assertEqual(
            server.infer_location(title="BRUNOY - ECOLE MATERNELLE DES MARDELLES"),
            "Brunoy",
        )

    def test_infer_location_from_project_slug(self):
        self.assertEqual(
            server.infer_location(url="https://example.com/projets/issy-les-moulineaux-construction-de-18-logements"),
            "Issy-Les-Moulineaux",
        )

    def test_prismic_raw_samples_include_inferred_location(self):
        doc = {"uid": "brunoy-ecole-maternelle", "data": {"titre_projet": "BRUNOY - ECOLE"}}
        samples = server.prismic_doc_to_raw_samples(doc, "https://example.com/projets/brunoy-ecole-maternelle")
        self.assertEqual(samples["lieu_inferé"]["suggested_field"], "lieu")
        self.assertEqual(samples["lieu_inferé"]["sample_values"], ["Brunoy"])

    def test_build_target_mapping_rows_carries_parsed_examples(self):
        fields = [{
            "source_label": "SURFACE",
            "sample_values": ["1 350 m2"],
            "suggested_field": "surface_m2",
            "confidence": 0.9,
        }]
        rows = server.build_target_mapping_rows(fields, {"programme": ["Équipement public"]})
        by_target = {row["target_field"]: row for row in rows}
        self.assertEqual(by_target["surface_m2"]["sample_values"], ["1 350 m2"])
        self.assertEqual(by_target["programme"]["target_examples"], ["Équipement public"])

    def test_sample_url_can_limit_mapping_analysis_scope(self):
        all_urls = ["https://example.com/a", "https://example.com/b"]
        chosen = server._canonical_url("https://example.com/b", "https://example.com/listing")
        selected = [chosen] if chosen in {server._canonical_url(u, "https://example.com/listing") for u in all_urls} else []
        self.assertEqual(selected, ["https://example.com/b"])


if __name__ == "__main__":
    unittest.main()
