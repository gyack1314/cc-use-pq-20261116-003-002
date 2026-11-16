"""换算分析服务。

流程严格遵循"先身份、后换算"：

1. 每个样本对每个目标指标先查解释版本里的身份映射；同名定义变更
   （``equivalent=false``）直接排除，即使单位可以换算也不合并。
2. 身份成立后才在换算图中寻找有证据的换算路径。
3. 换算后的数值再做指标有效范围校验；缺测、无映射、无换算、超范围
   全部记录排除原因，不进入统计，也不计入样本数。
4. 质量门禁只按实际可比样本计算。

分析冻结解释版本、请求摘要和来源修订；解释版本或规格更新后旧分析
自动失效（stale），评审与发布复用与主流程一致的拦截规则。已发布
报告永久保留其原依据。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, localcontext
import json
from typing import Any

from .analysis import statistics
from .domain import (
    DomainError,
    digest,
    require_identifier,
    utc_now,
)
from .metrics import INCLUDED, ConversionPath, IndicatorDefinition, Interpretation
from .quality import validate_window
from .store import Store


def harmonized_statistics(values: list[Decimal]) -> dict[str, Any] | None:
    return statistics(values)


def _converted_entry(sample: dict[str, Any], metric: str, indicator: IndicatorDefinition,
                     path: ConversionPath) -> dict[str, Any]:
    raw = Decimal(sample["measurements"][metric])
    converted = path.apply(raw)
    return {"sample_id": sample["sample_id"], "group_id": sample["group_id"],
            "metric": metric, "spec_version": sample["spec_version"],
            "original_value": str(raw),
            "original_unit": sample["measurement_units"][metric],
            "converted_value": str(converted), "indicator_unit": indicator.unit,
            "conversion": path.to_dict()}


class Harmonizer:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ build
    def build(self, campaign_id: str, analysis_id: str, start: str, end: str,
              indicators: Any, interpretation_version: int | None,
              actor: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(analysis_id, "analysis_id")
        require_identifier(actor, "actor")
        if not isinstance(indicators, list) or not indicators or len(indicators) > 24:
            raise DomainError("invalid_indicators", "request must name 1-24 target indicators")
        indicator_ids = [require_identifier(item, "indicator_id") for item in indicators]
        if len(set(indicator_ids)) != len(indicator_ids):
            raise DomainError("invalid_indicators", "target indicators must be unique")
        with self.store.transaction() as db:
            spec = self.store.get_spec(campaign_id, db=db)
            left, right = validate_window(spec, start, end)
            interpretation = self.store.get_interpretation(campaign_id, interpretation_version, db)
            for indicator_id in indicator_ids:
                interpretation.indicator(indicator_id)
            request = {"campaign_id": campaign_id, "start": left, "end": right,
                       "indicators": indicator_ids,
                       "interpretation_version": interpretation.version,
                       "interpretation_sha": digest(interpretation.to_dict())}
            request_sha = digest(request)
            old = db.execute("SELECT request_sha,source_revision,body FROM harmonized_analyses"
                             " WHERE campaign_id=? AND analysis_id=?",
                             (campaign_id, analysis_id)).fetchone()
            current = self.store.get_campaign(campaign_id, db)
            if old:
                if old["request_sha"] != request_sha:
                    raise DomainError("analysis_conflict", "analysis id names a different request")
                body = json.loads(old["body"])
                return {**body, "replayed": True,
                        "current": old["source_revision"] == current["revision"]}

            samples = self.store.list_samples(campaign_id, start=left, end=right, db=db)
            active_samples = [sample for sample in samples if sample["status"] == "active"]
            per_indicator = {}
            for indicator_id in indicator_ids:
                indicator = interpretation.indicator(indicator_id)
                included: list[dict[str, Any]] = []
                excluded: list[dict[str, Any]] = []
                values_by_group: dict[str, list[Decimal]] = defaultdict(list)
                for sample in active_samples:
                    disposition = self._judge_sample(
                        interpretation, indicator, sample, excluded)
                    if disposition is None:
                        continue
                    included.append(disposition["entry"])
                    values_by_group[disposition["entry"]["group_id"]].append(
                        Decimal(disposition["entry"]["converted_value"]))
                groups = {}
                for group in spec.groups:
                    groups[group] = harmonized_statistics(values_by_group[group])
                gates = self._gates(spec, indicator, included, excluded)
                reasons = Counter(item["reason"] for item in excluded)
                per_indicator[indicator_id] = {
                    "indicator": indicator.to_dict(),
                    "included_count": len(included),
                    "excluded_count": len(excluded),
                    "group_counts": {group: len(values_by_group[group]) for group in spec.groups},
                    "exclusion_reasons": dict(sorted(reasons.items())),
                    "gates": gates, "passed": not gates,
                    "groups": groups, "included": included, "excluded": excluded}
            quality = self._overall_quality(spec, left, right, active_samples, per_indicator)
            result = {"campaign_id": campaign_id, "analysis_id": analysis_id,
                      "request": request, "interpretation_version": interpretation.version,
                      "source_revision": current["revision"],
                      "quality": quality, "indicators": per_indicator,
                      "contrasts": self._contrasts(spec, per_indicator),
                      "created_at": utc_now(), "replayed": False, "current": True}
            self.store.save_harmonized_analysis(request, result, interpretation.version, actor, db=db)
            return result

    @staticmethod
    def _judge_sample(interpretation: Interpretation, indicator: IndicatorDefinition,
                      sample: dict[str, Any], excluded: list[dict[str, Any]]) -> dict[str, Any] | None:
        # 先按指标名找到样本上任何映射到该指标的测量，再按规格版本与报送
        # 单位解析身份；身份证据不足与缺测是两种不同的排除原因。
        metric_names = {
            mapping.metric for mapping in interpretation.mappings
            if mapping.indicator_id == indicator.indicator_id
            and mapping.metric in sample["measurements"]
        }
        if not metric_names:
            excluded.append({"sample_id": sample["sample_id"], "group_id": sample["group_id"],
                             "indicator_id": indicator.indicator_id,
                             "reason": "missing_measurement",
                             "detail": "no measurement mapped to this indicator for the sample's spec version"})
            return None
        candidates = [
            name for name in metric_names
            if interpretation.resolve_mapping(
                name, sample["spec_version"], sample["measurement_units"][name]) is not None
        ]
        if not candidates:
            metric = next(iter(metric_names))
            excluded.append({"sample_id": sample["sample_id"], "group_id": sample["group_id"],
                             "indicator_id": indicator.indicator_id, "metric": metric,
                             "reason": "no_identity_mapping",
                             "detail": {"spec_version": sample["spec_version"],
                                        "source_unit": sample["measurement_units"][metric]}})
            return None
        if len(candidates) > 1:
            excluded.append({"sample_id": sample["sample_id"], "group_id": sample["group_id"],
                             "indicator_id": indicator.indicator_id,
                             "reason": "ambiguous_mapping",
                             "detail": {"candidate_metrics": sorted(candidates)}})
            return None
        metric = candidates[0]
        source_unit = sample["measurement_units"][metric]
        verdict, evidence = interpretation.judge(
            metric, sample["spec_version"], source_unit, indicator.indicator_id)
        if verdict != INCLUDED:
            excluded.append({"sample_id": sample["sample_id"], "group_id": sample["group_id"],
                             "indicator_id": indicator.indicator_id, "metric": metric,
                             "reason": verdict, "detail": evidence})
            return None
        path = evidence["path"]
        entry = _converted_entry(sample, metric, indicator, path)
        converted = Decimal(entry["converted_value"])
        if not indicator.contains(converted):
            excluded.append({"sample_id": sample["sample_id"], "group_id": sample["group_id"],
                             "indicator_id": indicator.indicator_id, "metric": metric,
                             "reason": "out_of_harmonized_range",
                             "detail": {"converted_value": str(converted),
                                        "indicator_unit": indicator.unit,
                                        "minimum": str(indicator.minimum),
                                        "maximum": str(indicator.maximum)}})
            return None
        return {"entry": entry}

    @staticmethod
    def _gates(spec, indicator: IndicatorDefinition, included: list[dict[str, Any]],
               excluded: list[dict[str, Any]]) -> list[str]:
        gates: list[str] = []
        counts = Counter(entry["group_id"] for entry in included)
        for group in spec.groups:
            if counts[group] < spec.min_samples:
                gates.append(f"insufficient_comparable_samples:{group}")
        denominator = len(included) + len(excluded)
        if denominator and Decimal(len(excluded)) / Decimal(denominator) > spec.max_bad_fraction:
            gates.append("excess_exclusion_fraction")
        if not included:
            gates.append("no_comparable_samples")
        return gates

    @staticmethod
    def _overall_quality(spec, left: str, right: str, active_samples: list[dict[str, Any]],
                         per_indicator: dict[str, Any]) -> dict[str, Any]:
        gates = []
        for indicator_id, report in per_indicator.items():
            gates.extend(f"{indicator_id}:{gate}" for gate in report["gates"])
        all_included = {
            (indicator_id, entry["sample_id"])
            for indicator_id, report in per_indicator.items()
            for entry in report["included"]
        }
        # 实际可比样本：至少在一个目标指标上入统的样本。
        comparable_sample_ids = {sample_id for _, sample_id in all_included}
        return {"window": {"start": left, "end": right},
                "active_sample_count": len(active_samples),
                "comparable_sample_count": len(comparable_sample_ids),
                "indicator_gates": gates, "passed": not gates}

    @staticmethod
    def _contrasts(spec, per_indicator: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for group in spec.groups:
            if group == spec.baseline:
                continue
            metrics = {}
            for indicator_id, report in per_indicator.items():
                reference = report["groups"][spec.baseline]
                candidate = report["groups"][group]
                if reference is None or candidate is None:
                    metrics[indicator_id] = {"difference": None, "relative_percent": None}
                    continue
                mean_a = Decimal(reference["mean"])
                mean_b = Decimal(candidate["mean"])
                difference = mean_b - mean_a
                relative = None if mean_a == 0 else str(difference / abs(mean_a) * Decimal(100))
                metrics[indicator_id] = {"difference": str(difference),
                                         "relative_percent": relative,
                                         "unit": report["indicator"]["unit"]}
            result[group] = metrics
        return result

    # ---------------------------------------------------------------- status
    def status(self, campaign_id: str, analysis_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            analysis = self.store.get_harmonized_analysis(campaign_id, analysis_id, db)
            campaign = self.store.get_campaign(campaign_id, db)
            latest = db.execute("SELECT max(version) AS version FROM interpretations"
                                " WHERE campaign_id=?", (campaign_id,)).fetchone()["version"]
            return {"analysis_id": analysis_id,
                    "source_revision": analysis["source_revision"],
                    "current_revision": campaign["revision"],
                    "interpretation_version": analysis["interpretation_version"],
                    "latest_interpretation_version": latest,
                    "current": (analysis["source_revision"] == campaign["revision"]
                                and analysis["interpretation_version"] == latest),
                    "result_sha": analysis["result_sha"]}

    # ---------------------------------------------------------------- review
    def review(self, campaign_id: str, analysis_id: str, reviewer: str,
               decision: str, note: str, expected_sha: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(analysis_id, "analysis_id")
        require_identifier(reviewer, "reviewer")
        if decision not in {"approve", "reject"}:
            raise DomainError("invalid_decision", "decision must be approve or reject")
        if not isinstance(note, str) or not 8 <= len(note.strip()) <= 500:
            raise DomainError("invalid_note", "review note must have 8-500 characters")
        with self.store.transaction() as db:
            analysis = self.store.get_harmonized_analysis(campaign_id, analysis_id, db)
            campaign = self.store.get_campaign(campaign_id, db)
            if analysis["result_sha"] != expected_sha:
                raise DomainError("version_conflict", "analysis digest changed")
            if analysis["source_revision"] != campaign["revision"]:
                raise DomainError("stale_analysis", "analysis no longer matches source revision")
            latest = db.execute("SELECT max(version) AS version FROM interpretations"
                                " WHERE campaign_id=?", (campaign_id,)).fetchone()["version"]
            if analysis["interpretation_version"] != latest:
                raise DomainError("stale_analysis",
                                  "analysis interpretation version is no longer current")
            if not analysis["body"]["quality"]["passed"]:
                raise DomainError("quality_failed", "quality gates prevent review")
            return self.store.save_harmonized_review(
                campaign_id, analysis_id, reviewer, decision, note.strip(), expected_sha, reviewer, db=db)

    # --------------------------------------------------------------- publish
    def publish(self, campaign_id: str, analysis_id: str, release_id: str,
                actor: str, expected_sha: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        require_identifier(analysis_id, "analysis_id")
        require_identifier(release_id, "release_id")
        require_identifier(actor, "actor")
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM harmonized_releases WHERE campaign_id=? AND release_id=?",
                                  (campaign_id, release_id)).fetchone()
            if existing:
                if existing["analysis_id"] == analysis_id and existing["result_sha"] == expected_sha:
                    return {**json.loads(existing["body"]), "replayed": True}
                raise DomainError("release_conflict", "release id already names different content")
            analysis = self.store.get_harmonized_analysis(campaign_id, analysis_id, db)
            campaign = self.store.get_campaign(campaign_id, db)
            if analysis["result_sha"] != expected_sha:
                raise DomainError("version_conflict", "analysis digest changed")
            if analysis["source_revision"] != campaign["revision"]:
                raise DomainError("stale_analysis", "analysis no longer matches source revision")
            latest = db.execute("SELECT max(version) AS version FROM interpretations"
                                " WHERE campaign_id=?", (campaign_id,)).fetchone()["version"]
            if analysis["interpretation_version"] != latest:
                raise DomainError("stale_analysis",
                                  "analysis interpretation version is no longer current")
            if not analysis["body"]["quality"]["passed"]:
                raise DomainError("quality_failed", "quality gates prevent publication")
            decisions = db.execute("SELECT reviewer,decision,note,result_sha FROM harmonized_reviews"
                                   " WHERE campaign_id=? AND analysis_id=? ORDER BY reviewer",
                                   (campaign_id, analysis_id)).fetchall()
            if any(row["decision"] == "reject" for row in decisions):
                raise DomainError("review_rejected", "a reviewer rejected the analysis")
            approved = [dict(row) for row in decisions
                        if row["decision"] == "approve" and row["result_sha"] == expected_sha]
            if len(approved) < 2:
                raise DomainError("reviews_missing", "two independent approvals are required")
            if actor in {row["reviewer"] for row in approved}:
                raise DomainError("self_publish", "publisher cannot be an approving reviewer")
            result = {"campaign_id": campaign_id, "release_id": release_id,
                      "analysis_id": analysis_id,
                      "interpretation_version": analysis["interpretation_version"],
                      "source_revision": analysis["source_revision"],
                      "result_sha": expected_sha,
                      "approved_by": [row["reviewer"] for row in approved],
                      "published_by": actor, "created_at": utc_now(),
                      "analysis": analysis["body"], "replayed": False}
            self.store.save_harmonized_release(result, actor, db=db)
            return result

    # --------------------------------------------------------------- compare
    def compare(self, campaign_id: str, first_id: str, second_id: str) -> dict[str, Any]:
        """新旧换算分析对照，把差异分解为数值尺度效应与人群变化。

        定义断裂（指标身份或解释依据不一致）时拒绝数值合并，只给定性说明；
        同一指标、同一解释依据下，均值差分解为：

        * ``scale_effect``：同一批样本在新旧解释下因换算尺度不同产生的差；
        * ``population_effect``：样本人群变化（新增/移除样本）产生的差。
        """
        require_identifier(campaign_id, "campaign_id")
        require_identifier(first_id, "first_id")
        require_identifier(second_id, "second_id")
        with self.store.connect() as db:
            first = self.store.get_harmonized_analysis(campaign_id, first_id, db)["body"]
            second = self.store.get_harmonized_analysis(campaign_id, second_id, db)["body"]
            if first["request"]["start"] != second["request"]["start"] or \
                    first["request"]["end"] != second["request"]["end"]:
                raise DomainError("incomparable_analyses", "analyses must share the same window")
            report = {"campaign_id": campaign_id, "first_id": first_id, "second_id": second_id,
                      "interpretation_before": first["interpretation_version"],
                      "interpretation_after": second["interpretation_version"],
                      "quality_before": first["quality"], "quality_after": second["quality"],
                      "indicators": {}}
            shared = sorted(set(first["indicators"]) & set(second["indicators"]))
            for indicator_id in shared:
                report["indicators"][indicator_id] = self._compare_indicator(
                    indicator_id, first["indicators"][indicator_id],
                    second["indicators"][indicator_id])
            return report

    @staticmethod
    def _compare_indicator(indicator_id: str, before: dict[str, Any],
                           after: dict[str, Any]) -> dict[str, Any]:
        identity_broken = (
            before["indicator"]["definition_version"] != after["indicator"]["definition_version"]
            or before["indicator"]["unit"] != after["indicator"]["unit"]
        )
        before_ids = {entry["sample_id"] for entry in before["included"]}
        after_ids = {entry["sample_id"] for entry in after["included"]}
        population = {
            "added_samples": sorted(after_ids - before_ids),
            "removed_samples": sorted(before_ids - after_ids),
            "shared_samples": sorted(before_ids & after_ids),
        }
        result = {"indicator_id": indicator_id, "identity_broken": identity_broken,
                  "population_change": population,
                  "excluded_before": before["excluded_count"],
                  "excluded_after": after["excluded_count"], "groups": {}}
        if identity_broken:
            result["note"] = ("indicator definition or reporting unit changed; values are not merged, "
                              "any numeric difference mixes definition change and cannot be attributed")
            return result
        before_lookup = {entry["sample_id"]: entry for entry in before["included"]}
        after_lookup = {entry["sample_id"]: entry for entry in after["included"]}
        for group in before["groups"]:
            old_stats = before["groups"][group]
            new_stats = after["groups"][group]
            if old_stats is None or new_stats is None:
                result["groups"][group] = {"mean_before": old_stats and old_stats["mean"],
                                           "mean_after": new_stats and new_stats["mean"],
                                           "scale_effect": None, "population_effect": None}
                continue
            shared_ids = sorted(
                (before_ids & after_ids)
                & {entry["sample_id"] for entry in before["included"] if entry["group_id"] == group})
            scale = Harmonizer._scale_effect(shared_ids, before_lookup, after_lookup)
            mean_delta = Decimal(new_stats["mean"]) - Decimal(old_stats["mean"])
            population_effect = mean_delta - scale if scale is not None else None
            result["groups"][group] = {
                "mean_before": old_stats["mean"], "mean_after": new_stats["mean"],
                "mean_delta": str(mean_delta),
                "scale_effect": str(scale) if scale is not None else None,
                "population_effect": str(population_effect) if population_effect is not None else None,
                "conversion_rule_before": sorted({
                    entry["conversion"]["rule_id"]
                    for entry in before["included"] if entry["group_id"] == group}),
                "conversion_rule_after": sorted({
                    entry["conversion"]["rule_id"]
                    for entry in after["included"] if entry["group_id"] == group})}
        return result

    @staticmethod
    def _scale_effect(shared_ids: list[str], before_lookup: dict[str, Any],
                      after_lookup: dict[str, Any]) -> Decimal | None:
        if not shared_ids:
            return None
        with localcontext() as context:
            context.prec = 32
            deltas = [Decimal(after_lookup[sid]["converted_value"])
                      - Decimal(before_lookup[sid]["converted_value"]) for sid in shared_ids]
            return sum(deltas) / Decimal(len(deltas))

    # ------------------------------------------------------------- lifecycle
    def inventory(self, campaign_id: str) -> dict[str, Any]:
        require_identifier(campaign_id, "campaign_id")
        with self.store.connect() as db:
            campaign = self.store.get_campaign(campaign_id, db)
            rows = db.execute("SELECT analysis_id,interpretation_version,source_revision,body,created_at"
                              " FROM harmonized_analyses WHERE campaign_id=?"
                              " ORDER BY created_at,analysis_id", (campaign_id,)).fetchall()
            published = {row["analysis_id"] for row in db.execute(
                "SELECT analysis_id FROM harmonized_releases WHERE campaign_id=?", (campaign_id,))}
            latest = db.execute("SELECT max(version) AS version FROM interpretations"
                                " WHERE campaign_id=?", (campaign_id,)).fetchone()["version"]
            items = []
            for row in rows:
                body = json.loads(row["body"])
                reviews = db.execute("SELECT decision,count(*) AS count FROM harmonized_reviews"
                                     " WHERE campaign_id=? AND analysis_id=? GROUP BY decision",
                                     (campaign_id, row["analysis_id"])).fetchall()
                counts = {item["decision"]: item["count"] for item in reviews}
                if row["analysis_id"] in published:
                    state = "published"
                elif (row["source_revision"] != campaign["revision"]
                      or row["interpretation_version"] != latest):
                    state = "stale"
                elif not body["quality"]["passed"]:
                    state = "quality_blocked"
                elif counts.get("reject", 0):
                    state = "review_rejected"
                elif counts.get("approve", 0) >= 2:
                    state = "ready_to_publish"
                else:
                    state = "awaiting_reviews"
                items.append({"analysis_id": row["analysis_id"], "state": state,
                              "interpretation_version": row["interpretation_version"],
                              "source_revision": row["source_revision"],
                              "quality_gates": body["quality"]["indicator_gates"],
                              "reviews": counts, "created_at": row["created_at"]})
            return {"campaign_id": campaign_id, "current_revision": campaign["revision"],
                    "latest_interpretation_version": latest, "analyses": items,
                    "state_counts": dict(Counter(item["state"] for item in items))}
