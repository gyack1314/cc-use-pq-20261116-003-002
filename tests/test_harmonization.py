import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from atelier.domain import CampaignSpec, DomainError
from atelier.harmonization import Harmonizer
from atelier.ingest import Ingestor
from atelier.metrics import ConversionGraph, Interpretation
from atelier.reconcile import Reconciler
from atelier.store import Store


def load_fixture():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "cross_lab_experiment.json").read_text())


class ConversionGraphTests(unittest.TestCase):
    def test_reverse_rule_is_derived_and_multi_hop_is_refused(self):
        interpretation = Interpretation.from_dict("cell_capacity_trial", 1, load_fixture()["interpretations"]["v1"])
        graph = interpretation.graph()
        path = graph.find("mAh", "Ah")
        self.assertEqual(path.rule_id, "mah_to_ah")
        self.assertFalse(path.derived_reverse)
        reverse = graph.find("Ah", "mAh")
        self.assertTrue(reverse.derived_reverse)
        # 没有证据的单位对必须返回 None，而不是猜测链路。
        self.assertIsNone(graph.find("g", "Ah"))

    def test_duplicate_conversion_path_is_rejected(self):
        body = load_fixture()["interpretations"]["v1"]
        second = dict(body["conversions"][0])
        second["rule_id"] = "mah_to_ah_alt"
        second["basis"] = "Competing second source for the same unit pair"
        body["conversions"].append(second)
        with self.assertRaises(DomainError) as caught:
            Interpretation.from_dict("cell_capacity_trial", 1, body)
        self.assertIn(caught.exception.code, {"invalid_interpretation", "invalid_conversion"})
        self.assertIn("duplicate conversion path", str(caught.exception))

    def test_conversion_requires_evidence_basis(self):
        body = load_fixture()["interpretations"]["v1"]
        del body["conversions"][0]["basis"]
        with self.assertRaises(DomainError) as caught:
            Interpretation.from_dict("cell_capacity_trial", 1, body)
        self.assertEqual(caught.exception.code, "invalid_conversion")


class TwoStageJudgementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_fixture()
        cls.v1 = Interpretation.from_dict("cell_capacity_trial", 1, cls.data["interpretations"]["v1"])
        cls.v2 = Interpretation.from_dict("cell_capacity_trial", 1, cls.data["interpretations"]["v2_redefined"])

    def test_convertible_and_equivalent_is_included(self):
        verdict, evidence = self.v1.judge("capacity", 1, "mAh", "usable_capacity")
        self.assertEqual(verdict, "included")
        converted = evidence["path"].apply(__import__("decimal").Decimal("4850"))
        self.assertEqual(str(converted), "4.850")

    def test_same_name_definition_change_blocks_merge_even_when_convertible(self):
        # 单位依然可换算，但指标身份判定先失败，证明两个判断相互独立。
        self.assertIsNotNone(self.v2.graph().find("mAh", "Ah"))
        verdict, evidence = self.v2.judge("capacity", 1, "mAh", "usable_capacity")
        self.assertEqual(verdict, "definition_changed")
        self.assertEqual(evidence["definition_version"], "d2")

    def test_missing_mapping_is_insufficient_evidence(self):
        verdict, _ = self.v1.judge("capacity", 9, "mAh", "usable_capacity")
        self.assertEqual(verdict, "no_identity_mapping")

    def test_unit_without_rule_is_not_convertible(self):
        data = load_fixture()
        data["interpretations"]["v1"]["mappings"][1]["unit"] = "g"
        broken = Interpretation.from_dict("cell_capacity_trial", 1, data["interpretations"]["v1"])
        verdict, _ = broken.judge("capacity", 1, "g", "usable_capacity")
        self.assertEqual(verdict, "no_conversion_rule")


class HarmonizedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "harmonized.sqlite3")
        self.data = load_fixture()
        self.spec = CampaignSpec.from_dict(self.data["campaign"])
        self.store.create_campaign(self.spec, "operator")
        self.ingestor = Ingestor(self.store)
        self.harmonizer = Harmonizer(self.store)
        self.window = self.data["window"]
        for batch in self.data["batches"]:
            self.ingestor.ingest(self.spec.campaign_id, batch["batch_id"],
                                 batch["samples"], "operator")

    def interpretation(self, key="v1", version=1):
        body = self.data["interpretations"][key]
        parsed = Interpretation.from_dict(self.spec.campaign_id, version, body)
        self.store.create_interpretation(parsed, "metrologist")
        return parsed

    def build(self, analysis_id="harmony_one", version=1):
        return self.harmonizer.build(
            self.spec.campaign_id, analysis_id, self.window["start"], self.window["end"],
            ["usable_capacity"], version, "analyst")

    def approve(self, analysis):
        for reviewer in ("reviewer_a", "reviewer_b"):
            self.harmonizer.review(self.spec.campaign_id, analysis["analysis_id"], reviewer,
                                   "approve", "Cross-lab identity and conversions checked",
                                   analysis["result_sha"])

    def test_original_value_unit_and_spec_are_preserved(self):
        rows = self.store.list_samples(self.spec.campaign_id)
        samples = {row["sample_id"]: row for row in rows}
        self.assertIn("beta_1", samples)
        beta = samples["beta_1"]
        self.assertEqual(beta["measurements"]["capacity"], "4850")
        self.assertEqual(beta["measurement_units"]["capacity"], "mAh")
        self.assertEqual(beta["spec_version"], 1)
        alpha = samples["alpha_1"]
        self.assertEqual(alpha["measurements"]["capacity"], "4.80")
        self.assertEqual(alpha["measurement_units"]["capacity"], "Ah")
    def test_plain_numeric_measurements_remain_backward_compatible(self):
        sample = {"sample_id": "plain_one", "group_id": "control",
                  "event_time": "2026-02-01T08:00:00Z",
                  "measurements": {"capacity": "4.70"}}
        result = self.ingestor.ingest(self.spec.campaign_id, "plain_batch", [sample], "operator")
        self.assertEqual(result["accepted"], ["plain_one"])
        stored = self.store.list_samples(self.spec.campaign_id)
        record = next(row for row in stored if row["sample_id"] == "plain_one")
        self.assertEqual(record["measurement_units"]["capacity"], "Ah")

    def test_intake_rejects_unit_not_accepted_by_spec(self):
        sample = {"sample_id": "bad_unit", "group_id": "control",
                  "event_time": "2026-02-01T08:00:00Z",
                  "measurements": {"capacity": {"value": "4.70", "unit": "g"}}}
        result = self.ingestor.ingest(self.spec.campaign_id, "bad_unit_batch", [sample], "operator")
        self.assertEqual(result["rejected"][0]["reason"], "invalid_unit")

    def test_harmonized_analysis_converts_and_keeps_provenance(self):
        self.interpretation()
        result = self.build()
        report = result["indicators"]["usable_capacity"]
        self.assertEqual(report["included_count"], 6)
        self.assertEqual(report["excluded_count"], 0)
        self.assertEqual(report["group_counts"], {"control": 3, "process_b": 3})
        beta = next(entry for entry in report["included"] if entry["sample_id"] == "beta_1")
        self.assertEqual(beta["original_value"], "4850")
        self.assertEqual(beta["original_unit"], "mAh")
        self.assertEqual(beta["converted_value"], "4.850")
        self.assertEqual(beta["conversion"]["rule_id"], "mah_to_ah")
        self.assertTrue(result["quality"]["passed"])
        # 换算后的统一尺度：control 均值 (4.80+4.90+4.85)/3
        self.assertEqual(Decimal(report["groups"]["control"]["mean"]), Decimal("4.85"))
        self.assertEqual(Decimal(report["groups"]["process_b"]["mean"]), Decimal("5.2"))

    def test_definition_change_excludes_every_sample_with_reasons(self):
        self.interpretation("v2_redefined")
        result = self.build()
        report = result["indicators"]["usable_capacity"]
        self.assertEqual(report["included_count"], 0)
        self.assertEqual(report["excluded_count"], 6)
        self.assertTrue(all(row["reason"] == "definition_changed" for row in report["excluded"]))
        self.assertIn("no_comparable_samples", report["gates"])
        self.assertFalse(result["quality"]["passed"])

    def test_missing_measurement_is_excluded_not_silently_dropped(self):
        self.interpretation()
        # 将指标改为可选后，纳入一个完全缺测的样本；它必须带原因出现在排除清单。
        data = load_fixture()
        data["campaign"]["metrics"][0]["required"] = False
        self.store.update_spec(self.spec.campaign_id, data["campaign"], 1, "operator")
        sparse = {"sample_id": "sparse_1", "group_id": "control",
                  "event_time": "2026-02-01T09:00:00Z", "measurements": {}}
        self.ingestor.ingest(self.spec.campaign_id, "sparse_batch", [sparse], "operator")
        result = self.build()
        report = result["indicators"]["usable_capacity"]
        reasons = {row["sample_id"]: row["reason"] for row in report["excluded"]}
        self.assertEqual(reasons["sparse_1"], "missing_measurement")
        self.assertEqual(report["included_count"], 6)

    def test_sample_at_unmapped_spec_version_is_insufficient_evidence(self):
        self.interpretation()
        # 规格推进到 v2 后入库的样本，解释版本只对 v1 给出映射，因此无身份证据。
        updated = json.loads(json.dumps(self.data["campaign"]))
        updated["metrics"][0]["precision"] = 5
        self.store.update_spec(self.spec.campaign_id, updated, 1, "operator")
        new_sample = {"sample_id": "new_wave_1", "group_id": "control",
                      "event_time": "2026-02-01T10:00:00Z",
                      "measurements": {"capacity": {"value": "4.70000", "unit": "Ah"}}}
        self.ingestor.ingest(self.spec.campaign_id, "new_wave_batch", [new_sample], "operator")
        result = self.build()
        report = result["indicators"]["usable_capacity"]
        reasons = {row["sample_id"]: row["reason"] for row in report["excluded"]}
        self.assertEqual(reasons["new_wave_1"], "no_identity_mapping")
        self.assertEqual(report["included_count"], 6)

    def test_out_of_harmonized_range_after_conversion_is_excluded(self):
        body = self.data["interpretations"]["v1"]
        body["indicators"][0]["maximum"] = "5.0"
        parsed = Interpretation.from_dict(self.spec.campaign_id, 1, body)
        self.store.create_interpretation(parsed, "metrologist")
        result = self.build()
        report = result["indicators"]["usable_capacity"]
        excluded = [row for row in report["excluded"]
                    if row["reason"] == "out_of_harmonized_range"]
        # 5.20 Ah、5.25 Ah、5.15 Ah 三个 process_b 样本换算后超界。
        self.assertEqual(sorted(row["sample_id"] for row in excluded),
                         ["alpha_3", "beta_2", "beta_3"])
        self.assertIn("insufficient_comparable_samples:process_b", report["gates"])
        self.assertFalse(result["quality"]["passed"])

    def test_quality_gate_uses_only_actual_comparable_samples(self):
        body = self.data["interpretations"]["v1"]
        body["indicators"][0]["maximum"] = "5.0"
        parsed = Interpretation.from_dict(self.spec.campaign_id, 1, body)
        self.store.create_interpretation(parsed, "metrologist")
        result = self.build()
        self.assertEqual(result["quality"]["comparable_sample_count"], 3)
        self.assertFalse(result["quality"]["passed"])
        with self.assertRaises(DomainError) as caught:
            self.approve(result)
        self.assertEqual(caught.exception.code, "quality_failed")

    def test_review_and_publish_flow_with_frozen_basis(self):
        self.interpretation()
        analysis = self.build()
        self.approve(analysis)
        release = self.harmonizer.publish(
            self.spec.campaign_id, "harmony_one", "harm_release_one",
            "publisher", analysis["result_sha"])
        self.assertEqual(release["interpretation_version"], 1)
        self.assertEqual(release["approved_by"], ["reviewer_a", "reviewer_b"])
        self.assertTrue(Reconciler(self.store).full_report(self.spec.campaign_id)["ok"])

    def test_new_interpretation_versions_stale_analysis_and_preserve_release(self):
        self.interpretation()
        analysis = self.build()
        self.approve(analysis)
        release = self.harmonizer.publish(
            self.spec.campaign_id, "harmony_one", "harm_release_one",
            "publisher", analysis["result_sha"])
        # 依据更新：新解释版本推进 revision，旧分析变 stale。
        self.interpretation("v2_redefined", version=2)
        status = self.harmonizer.status(self.spec.campaign_id, "harmony_one")
        self.assertFalse(status["current"])
        with self.assertRaises(DomainError) as caught:
            self.harmonizer.review(
                self.spec.campaign_id, "harmony_one", "reviewer_c", "approve",
                "Attempting review after basis update", analysis["result_sha"])
        self.assertEqual(caught.exception.code, "stale_analysis")
        # 已发布报告保留原依据，不被重写。
        stored = self.store.get_harmonized_release(self.spec.campaign_id, "harm_release_one")
        self.assertEqual(stored["body"]["release_sha"], release["release_sha"])
        self.assertEqual(stored["interpretation_version"], 1)

    def test_analysis_idempotence_and_conflict(self):
        self.interpretation()
        first = self.build()
        second = self.build()
        self.assertTrue(second["replayed"])
        self.assertEqual(first["result_sha"], second["result_sha"])
        with self.assertRaises(DomainError) as caught:
            self.harmonizer.build(
                self.spec.campaign_id, "harmony_one", self.window["start"],
                "2026-02-02T00:00:00Z", ["usable_capacity"], 1, "analyst")
        self.assertEqual(caught.exception.code, "analysis_conflict")

    def test_compare_decomposes_scale_and_population_effects(self):
        self.interpretation()
        before = self.build("harmony_before")
        # 解释版本 2：换算因子修正（1 mAh = 0.001002 Ah），身份不变。
        body = json.loads(json.dumps(self.data["interpretations"]["v1"]))
        body["conversions"][0]["factor"] = "0.001002"
        body["conversions"][0]["basis"] = "Calibration correction notice rev4"
        parsed = Interpretation.from_dict(self.spec.campaign_id, 2, body)
        self.store.create_interpretation(parsed, "metrologist")
        after = self.harmonizer.build(
            self.spec.campaign_id, "harmony_after", self.window["start"], self.window["end"],
            ["usable_capacity"], 2, "analyst")
        comparison = self.harmonizer.compare(
            self.spec.campaign_id, "harmony_before", "harmony_after")
        indicator = comparison["indicators"]["usable_capacity"]
        self.assertFalse(indicator["identity_broken"])
        control = indicator["groups"]["control"]
        # 两个 mAh 样本（4850）各增加 4850*0.000002 = 0.0097，Ah 样本不变；
        # 组内尺度效应为共享样本的平均换算差（0.0097/3）。
        self.assertAlmostEqual(Decimal(control["scale_effect"]),
                               Decimal("0.003233333333333333333333333333"))
        self.assertAlmostEqual(Decimal(control["population_effect"]), Decimal("0"))
        self.assertEqual(
            Decimal(before["indicators"]["usable_capacity"]["groups"]["control"]["mean"]),
            Decimal("4.85"))

    def test_compare_reports_population_change_and_refuses_definition_merge(self):
        self.interpretation()
        self.build("harmony_before")
        self.interpretation("v2_redefined", version=2)
        self.harmonizer.build(
            self.spec.campaign_id, "harmony_after", self.window["start"], self.window["end"],
            ["usable_capacity"], 2, "analyst")
        comparison = self.harmonizer.compare(
            self.spec.campaign_id, "harmony_before", "harmony_after")
        indicator = comparison["indicators"]["usable_capacity"]
        self.assertTrue(indicator["identity_broken"])
        self.assertIn("not merged", indicator["note"])
        self.assertEqual(indicator["excluded_after"], 6)

    def test_batch_replay_does_not_change_acceptance_facts(self):
        baseline = self.store.get_campaign(self.spec.campaign_id)["revision"]
        first = self.ingestor.ingest(
            self.spec.campaign_id, "lab_alpha_ah",
            self.data["batches"][0]["samples"], "operator")
        self.assertTrue(first["replayed"])
        self.assertEqual(self.store.get_campaign(self.spec.campaign_id)["revision"], baseline)
        # 解释版本更新推进账本修订，但批次的原始接收事实不变。
        self.interpretation()
        replayed = self.ingestor.ingest(
            self.spec.campaign_id, "lab_alpha_ah",
            self.data["batches"][0]["samples"], "operator")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["accepted"], first["accepted"])
        self.assertEqual(len(self.store.list_samples(self.spec.campaign_id)), 6)

    def test_lifecycle_inventory_tracks_states(self):
        self.interpretation()
        self.build()
        inventory = self.harmonizer.inventory(self.spec.campaign_id)
        self.assertEqual(inventory["state_counts"], {"awaiting_reviews": 1})
        self.assertEqual(inventory["latest_interpretation_version"], 1)


class SchemaMigrationTests(unittest.TestCase):
    def test_legacy_database_without_unit_column_is_upgraded(self):
        import sqlite3
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db_path = Path(folder.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(db_path)
        legacy.executescript(
            "CREATE TABLE campaigns(id TEXT PRIMARY KEY,version INTEGER NOT NULL,"
            "revision INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
            "CREATE TABLE specs(campaign_id TEXT NOT NULL,version INTEGER NOT NULL,body TEXT NOT NULL,"
            "content_sha TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(campaign_id,version));"
            "CREATE TABLE samples(campaign_id TEXT NOT NULL,sample_id TEXT NOT NULL,batch_id TEXT NOT NULL,"
            "group_id TEXT NOT NULL,event_time TEXT NOT NULL,spec_version INTEGER NOT NULL,"
            "measurements TEXT NOT NULL,content_sha TEXT NOT NULL,status TEXT NOT NULL,"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(campaign_id,sample_id));"
            "CREATE TABLE batches(campaign_id TEXT NOT NULL,batch_id TEXT NOT NULL,payload_sha TEXT NOT NULL,"
            "spec_version INTEGER NOT NULL,accepted INTEGER NOT NULL,rejected INTEGER NOT NULL,"
            "body TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(campaign_id,batch_id));"
            "CREATE TABLE rejections(campaign_id TEXT NOT NULL,batch_id TEXT NOT NULL,ordinal INTEGER NOT NULL,"
            "sample_id TEXT,reason TEXT NOT NULL,detail TEXT NOT NULL,event_time TEXT, "
            "PRIMARY KEY(campaign_id,batch_id,ordinal));"
            "CREATE TABLE audits(id INTEGER PRIMARY KEY AUTOINCREMENT,campaign_id TEXT NOT NULL,"
            "kind TEXT NOT NULL,actor TEXT NOT NULL,subject TEXT NOT NULL,before_json TEXT,after_json TEXT,"
            "at TEXT NOT NULL);"
            "CREATE TABLE analyses(campaign_id TEXT NOT NULL,analysis_id TEXT NOT NULL,request_sha TEXT NOT NULL,"
            "source_revision INTEGER NOT NULL,body TEXT NOT NULL,result_sha TEXT NOT NULL,created_at TEXT NOT NULL,"
            "PRIMARY KEY(campaign_id,analysis_id));"
            "CREATE TABLE reviews(campaign_id TEXT NOT NULL,analysis_id TEXT NOT NULL,reviewer TEXT NOT NULL,"
            "decision TEXT NOT NULL,note TEXT NOT NULL,result_sha TEXT NOT NULL,created_at TEXT NOT NULL,"
            "PRIMARY KEY(campaign_id,analysis_id,reviewer));"
            "CREATE TABLE releases(campaign_id TEXT NOT NULL,release_id TEXT NOT NULL,analysis_id TEXT NOT NULL,"
            "source_revision INTEGER NOT NULL,result_sha TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,"
            "PRIMARY KEY(campaign_id,release_id));")
        legacy.commit()
        legacy.close()
        store = Store(db_path)
        with store.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(samples)")}
            self.assertIn("measurement_units", columns)
            for table in ("interpretations", "harmonized_analyses",
                          "harmonized_reviews", "harmonized_releases"):
                self.assertIsNotNone(
                    db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                               (table,)).fetchone())
        data = load_fixture()
        spec = CampaignSpec.from_dict(data["campaign"])
        store.create_campaign(spec, "operator")
        Ingestor(store).ingest(spec.campaign_id, data["batches"][0]["batch_id"],
                               data["batches"][0]["samples"], "operator")
        rows = store.list_samples(spec.campaign_id)
        self.assertTrue(rows)
        self.assertIn("capacity", rows[0]["measurement_units"])


if __name__ == "__main__":
    unittest.main()
