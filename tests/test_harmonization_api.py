import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from atelier.api import create_app


def load_fixture():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "cross_lab_experiment.json").read_text())


class HarmonizedApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.client = TestClient(create_app(str(Path(self.folder.name) / "harm_api.sqlite3")))
        self.addCleanup(self.client.close)
        self.data = load_fixture()
        response = self.client.post("/campaigns",
                                    json={**self.data["campaign"], "actor": "operator"})
        self.assertEqual(response.status_code, 201, response.text)
        self.base = "/campaigns/cell_capacity_trial"
        for batch in self.data["batches"]:
            uploaded = self.client.post(self.base + "/batches/" + batch["batch_id"],
                                        json={"actor": "operator", "samples": batch["samples"]})
            self.assertEqual(uploaded.status_code, 200, uploaded.text)

    def create_interpretation(self, key="v1", version=1):
        response = self.client.post(self.base + "/interpretations",
                                    json={**self.data["interpretations"][key], "actor": "metrologist"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["version"], version)
        return response.json()

    def build(self, analysis_id="harmony_one", version=1):
        return self.client.post(self.base + "/harmonized-analyses/" + analysis_id,
                                json={**self.data["window"], "actor": "analyst",
                                      "indicators": ["usable_capacity"],
                                      "interpretation_version": version})

    def test_full_cross_lab_release_over_http(self):
        self.create_interpretation()
        analysis = self.build().json()
        report = analysis["indicators"]["usable_capacity"]
        self.assertEqual(report["included_count"], 6)
        beta = next(entry for entry in report["included"] if entry["sample_id"] == "beta_1")
        self.assertEqual(beta["original_unit"], "mAh")
        self.assertEqual(beta["converted_value"], "4.850")
        self.assertTrue(analysis["quality"]["passed"])
        for reviewer in ("reviewer_a", "reviewer_b"):
            review = self.client.post(
                self.base + "/harmonized-analyses/harmony_one/reviews",
                json={"reviewer": reviewer, "decision": "approve",
                      "note": "Cross-lab identity and conversions checked",
                      "expected_sha": analysis["result_sha"]})
            self.assertEqual(review.status_code, 200, review.text)
        release = self.client.post(self.base + "/harmonized-releases/harm_release_one",
                                   json={"analysis_id": "harmony_one", "actor": "publisher",
                                         "expected_sha": analysis["result_sha"]})
        self.assertEqual(release.status_code, 200, release.text)
        self.assertEqual(release.json()["interpretation_version"], 1)
        self.assertTrue(self.client.get(self.base + "/reconciliation").json()["ok"])
        evidence = self.client.get(self.base + "/evidence/export").json()
        self.assertIn("interpretations", evidence["payload"])
        self.assertEqual(len(evidence["payload"]["harmonized_analyses"]), 1)

    def test_definition_change_is_rejected_over_http(self):
        self.create_interpretation("v2_redefined")
        analysis = self.build().json()
        report = analysis["indicators"]["usable_capacity"]
        self.assertEqual(report["included_count"], 0)
        self.assertTrue(all(row["reason"] == "definition_changed" for row in report["excluded"]))
        self.assertFalse(analysis["quality"]["passed"])

    def test_interpretation_versioning_and_stale_review(self):
        self.create_interpretation()
        analysis = self.build().json()
        self.create_interpretation("v2_redefined", version=2)
        status = self.client.get(self.base + "/harmonized-analyses/harmony_one/status").json()
        self.assertFalse(status["current"])
        review = self.client.post(
            self.base + "/harmonized-analyses/harmony_one/reviews",
            json={"reviewer": "reviewer_a", "decision": "approve",
                  "note": "Cross-lab identity and conversions checked",
                  "expected_sha": analysis["result_sha"]})
        self.assertEqual(review.status_code, 409)
        self.assertEqual(review.json()["error"], "stale_analysis")

    def test_compare_endpoint_decomposes_differences(self):
        self.create_interpretation()
        self.build("harmony_before")
        body = json.loads(json.dumps(self.data["interpretations"]["v1"]))
        body["conversions"][0]["factor"] = "0.001002"
        body["conversions"][0]["basis"] = "Calibration correction notice rev4"
        response = self.client.post(self.base + "/interpretations",
                                    json={**body, "actor": "metrologist"})
        self.assertEqual(response.status_code, 201, response.text)
        self.client.post(self.base + "/harmonized-analyses/harmony_after",
                         json={**self.data["window"], "actor": "analyst",
                               "indicators": ["usable_capacity"],
                               "interpretation_version": 2})
        comparison = self.client.get(
            self.base + "/harmonized-analyses/harmony_before/compare/harmony_after").json()
        indicator = comparison["indicators"]["usable_capacity"]
        self.assertFalse(indicator["identity_broken"])
        self.assertIsNotNone(indicator["groups"]["control"]["scale_effect"])

    def test_unknown_indicator_is_a_validation_error(self):
        self.create_interpretation()
        response = self.client.post(self.base + "/harmonized-analyses/bad_one",
                                    json={**self.data["window"], "actor": "analyst",
                                          "indicators": ["not_declared"],
                                          "interpretation_version": 1})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "unknown_indicator")


if __name__ == "__main__":
    unittest.main()
