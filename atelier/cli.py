from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from .analysis import Analyzer
from .domain import CampaignSpec
from .harmonization import Harmonizer
from .ingest import Ingestor
from .metrics import Interpretation
from .release import Publisher
from .store import Store


def demo(fixture: Path) -> dict:
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "experiment.sqlite3")
        spec = CampaignSpec.from_dict(payload["campaign"])
        campaign = store.create_campaign(spec, "operator")
        ingestor = Ingestor(store)
        batches = []
        for batch in payload["batches"]:
            batches.append(ingestor.ingest(spec.campaign_id, batch["batch_id"],
                                           batch["samples"], "operator"))
        if "interpretations" in payload:
            return cross_lab_demo(store, spec, payload, batches, campaign)
        result = Analyzer(store).build(spec.campaign_id, "analysis_01",
                                       payload["window"]["start"], payload["window"]["end"],
                                       "analyst")
        if not result["quality"]["passed"]:
            raise RuntimeError("synthetic fixture did not pass quality gates")
        publisher = Publisher(store)
        for reviewer in ("reviewer_a", "reviewer_b"):
            publisher.review(spec.campaign_id, "analysis_01", reviewer,
                             "approve", "Synthetic measurements reviewed", result["result_sha"])
        release = publisher.publish(spec.campaign_id, "analysis_01", "release_01",
                                    "publisher", result["result_sha"])
        return {"campaign": campaign["campaign_id"], "accepted": sum(len(x["accepted"]) for x in batches),
                "quality": result["quality"], "contrasts": result["contrasts"],
                "release_sha": release["release_sha"], "audit_events": len(store.list_audits(spec.campaign_id))}


def cross_lab_demo(store: Store, spec: CampaignSpec, payload: dict, batches: list,
                   campaign: dict) -> dict:
    """跨实验室场景：先注册解释版本，再做换算分析与发布。"""
    key, body = next(iter(payload["interpretations"].items()))
    if key != "v1":
        raise RuntimeError("cross-lab demo fixture must define a v1 interpretation")
    interpretation = Interpretation.from_dict(spec.campaign_id, 1, body)
    store.create_interpretation(interpretation, "metrologist")
    indicator_ids = [item.indicator_id for item in interpretation.indicators]
    harmonizer = Harmonizer(store)
    result = harmonizer.build(spec.campaign_id, "harmonized_01",
                              payload["window"]["start"], payload["window"]["end"],
                              indicator_ids, 1, "analyst")
    if not result["quality"]["passed"]:
        raise RuntimeError("cross-lab fixture did not pass harmonized quality gates")
    for reviewer in ("reviewer_a", "reviewer_b"):
        harmonizer.review(spec.campaign_id, "harmonized_01", reviewer,
                          "approve", "Cross-lab identity and conversions checked",
                          result["result_sha"])
    release = harmonizer.publish(spec.campaign_id, "harmonized_01", "harm_release_01",
                                 "publisher", result["result_sha"])
    indicator_report = result["indicators"][indicator_ids[0]]
    return {
        "campaign": campaign["campaign_id"],
        "accepted": sum(len(x["accepted"]) for x in batches),
        "interpretation_version": result["interpretation_version"],
        "comparable_samples": result["quality"]["comparable_sample_count"],
        "included": indicator_report["included_count"],
        "excluded": indicator_report["exclusion_reasons"],
        "group_counts": indicator_report["group_counts"],
        "contrasts": result["contrasts"],
        "release_sha": release["release_sha"],
        "audit_events": len(store.list_audits(spec.campaign_id))}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an offline synthetic experiment demo")
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(demo(args.fixture), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
