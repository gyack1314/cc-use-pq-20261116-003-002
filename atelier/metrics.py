"""跨实验室指标解释与换算领域逻辑。

两个判断严格分开：

1. 可换算性（convertibility）：两个单位之间是否存在有证据的换算关系；
2. 指标同一性（comparability）：测量在语义上是否属于同一个指标定义。

单位能换算不代表指标可比——同名指标的定义变更必须通过 mapping 的
``equivalent=false`` 阻断合并。所有换算规则都携带证据文本（basis），
解释版本（Interpretation）整体不可变、内容寻址。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Any, Iterable

from .domain import (
    ALLOWED_UNITS,
    DomainError,
    decimal,
    integer,
    require_identifier,
    require_metric,
)

DEFINITION_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,15}$")

# 判定结论码：入统
INCLUDED = "included"
# 判定结论码：排除（证据不足或定义不一致，绝不凑样本数）
EXCLUSION_REASONS = (
    "missing_measurement",      # 缺测
    "no_identity_mapping",      # 解释版本未给出指标身份映射
    "definition_changed",       # 同名但定义变更，不可合并
    "no_conversion_rule",       # 单位之间没有有证据的换算关系
    "out_of_harmonized_range",  # 换算后超出指标有效范围
    "ambiguous_mapping",        # 同一指标存在多个候选测量，证据不足以二选一
)


def require_definition_version(value: Any) -> str:
    if not isinstance(value, str) or not DEFINITION_VERSION.fullmatch(value):
        raise DomainError("invalid_definition_version",
                          "definition_version must use 1-16 letters, digits, '.', '_' or '-'")
    return value


@dataclass(frozen=True)
class ConversionRule:
    rule_id: str
    from_unit: str
    to_unit: str
    factor: Decimal
    offset: Decimal
    basis: str

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "ConversionRule":
        if not isinstance(item, dict):
            raise DomainError("invalid_conversion", "conversion rule must be an object")
        rule_id = require_identifier(item.get("rule_id"), "rule_id")
        from_unit = item.get("from_unit")
        to_unit = item.get("to_unit")
        if from_unit not in ALLOWED_UNITS or to_unit not in ALLOWED_UNITS:
            raise DomainError("invalid_unit", f"conversion {rule_id} uses an unsupported unit")
        if from_unit == to_unit:
            raise DomainError("invalid_conversion", f"conversion {rule_id} needs distinct units")
        factor = decimal(item.get("factor"), "factor")
        if factor <= 0:
            raise DomainError("invalid_conversion", f"conversion {rule_id} factor must be positive")
        offset = decimal(item.get("offset", "0"), "offset")
        basis = item.get("basis")
        if not isinstance(basis, str) or not 3 <= len(basis.strip()) <= 300:
            raise DomainError("invalid_conversion", f"conversion {rule_id} needs evidence basis text")
        return cls(rule_id, from_unit, to_unit, factor, offset, basis.strip())

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "from_unit": self.from_unit, "to_unit": self.to_unit,
                "factor": str(self.factor), "offset": str(self.offset), "basis": self.basis}


@dataclass(frozen=True)
class IndicatorDefinition:
    indicator_id: str
    definition: str
    definition_version: str
    unit: str
    minimum: Decimal
    maximum: Decimal
    precision: int

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "IndicatorDefinition":
        if not isinstance(item, dict):
            raise DomainError("invalid_indicator", "indicator definition must be an object")
        indicator_id = require_identifier(item.get("indicator_id"), "indicator_id")
        definition = item.get("definition")
        if not isinstance(definition, str) or not 3 <= len(definition.strip()) <= 300:
            raise DomainError("invalid_indicator", f"indicator {indicator_id} needs definition text")
        definition_version = require_definition_version(item.get("definition_version"))
        unit = item.get("unit")
        if unit not in ALLOWED_UNITS:
            raise DomainError("invalid_unit", f"indicator {indicator_id} has unsupported unit")
        minimum = decimal(item.get("minimum"), "minimum")
        maximum = decimal(item.get("maximum"), "maximum")
        if minimum >= maximum:
            raise DomainError("invalid_indicator", f"indicator {indicator_id} bounds are reversed")
        precision = integer(item.get("precision", 4), "precision", 0, 9)
        return cls(indicator_id, definition.strip(), definition_version, unit,
                   minimum, maximum, precision)

    def to_dict(self) -> dict[str, Any]:
        return {"indicator_id": self.indicator_id, "definition": self.definition,
                "definition_version": self.definition_version, "unit": self.unit,
                "minimum": str(self.minimum), "maximum": str(self.maximum), "precision": self.precision}

    def contains(self, value: Decimal) -> bool:
        return self.minimum <= value <= self.maximum


@dataclass(frozen=True)
class MetricMapping:
    metric: str
    unit: str
    spec_versions: frozenset[int]
    indicator_id: str
    definition_version: str
    equivalent: bool
    reason: str | None

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "MetricMapping":
        if not isinstance(item, dict):
            raise DomainError("invalid_mapping", "metric mapping must be an object")
        metric = require_metric(item.get("metric"))
        unit = item.get("unit")
        if unit not in ALLOWED_UNITS:
            raise DomainError("invalid_unit", f"mapping for {metric} uses an unsupported unit")
        versions = item.get("spec_versions")
        if not isinstance(versions, list) or not versions:
            raise DomainError("invalid_mapping", f"mapping for {metric} needs spec_versions")
        spec_versions: list[int] = []
        for version in versions:
            spec_versions.append(integer(version, "spec_version", 1, 1_000_000))
        if len(set(spec_versions)) != len(spec_versions):
            raise DomainError("invalid_mapping", f"mapping for {metric} repeats spec versions")
        indicator_id = require_identifier(item.get("indicator_id"), "indicator_id")
        definition_version = require_definition_version(item.get("definition_version"))
        equivalent = item.get("equivalent")
        if not isinstance(equivalent, bool):
            raise DomainError("invalid_mapping", "equivalent must be boolean")
        reason = item.get("reason")
        if reason is not None:
            if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 300:
                raise DomainError("invalid_mapping", "mapping reason must have 3-300 characters")
            reason = reason.strip()
        if not equivalent and reason is None:
            raise DomainError("invalid_mapping",
                              f"mapping for {metric} must explain why the definition is not equivalent")
        return cls(metric, unit, frozenset(spec_versions), indicator_id,
                   definition_version, equivalent, reason)

    def to_dict(self) -> dict[str, Any]:
        result = {"metric": self.metric, "unit": self.unit,
                  "spec_versions": sorted(self.spec_versions),
                  "indicator_id": self.indicator_id, "definition_version": self.definition_version,
                  "equivalent": self.equivalent}
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class ConversionPath:
    """一次单位换算的完整证据链。"""

    rule_id: str
    factor: Decimal
    offset: Decimal
    derived_reverse: bool

    def apply(self, value: Decimal) -> Decimal:
        return value * self.factor + self.offset

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "factor": str(self.factor),
                "offset": str(self.offset), "derived_reverse": self.derived_reverse}


class ConversionGraph:
    """单位换算图：只承认显式规则及其逆规则，不做多级链路猜测。

    多级链路会复合未被单独评审的误差，证据不足时应直接判不可换算并排除。
    """

    def __init__(self, rules: Iterable[ConversionRule]):
        self.edges: dict[tuple[str, str], ConversionPath] = {}
        for rule in rules:
            direct = ConversionPath(rule.rule_id, rule.factor, rule.offset, False)
            self._put(rule.from_unit, rule.to_unit, direct, rule.rule_id)
            # 逆规则：y = x*factor + offset  =>  x = (y - offset)/factor
            reverse = ConversionPath(rule.rule_id, Decimal(1) / rule.factor,
                                     -rule.offset / rule.factor, True)
            self._put(rule.to_unit, rule.from_unit, reverse, rule.rule_id)

    def _put(self, source: str, target: str, path: ConversionPath, rule_id: str) -> None:
        key = (source, target)
        if key in self.edges:
            raise DomainError("invalid_conversion", f"duplicate conversion path for {source}->{target}")
        self.edges[key] = path

    def find(self, source_unit: str, target_unit: str) -> ConversionPath | None:
        if source_unit == target_unit:
            return ConversionPath("identity", Decimal(1), Decimal(0), False)
        return self.edges.get((source_unit, target_unit))


@dataclass(frozen=True)
class Interpretation:
    campaign_id: str
    version: int
    indicators: tuple[IndicatorDefinition, ...]
    conversions: tuple[ConversionRule, ...]
    mappings: tuple[MetricMapping, ...]

    @classmethod
    def from_dict(cls, campaign_id: str, version: int, body: dict[str, Any]) -> "Interpretation":
        if not isinstance(body, dict):
            raise DomainError("invalid_interpretation", "interpretation must be an object")
        require_identifier(campaign_id, "campaign_id")
        integer(version, "version", 1, 1_000_000)
        indicator_items = body.get("indicators")
        if not isinstance(indicator_items, list) or not indicator_items:
            raise DomainError("invalid_interpretation", "interpretation needs at least one indicator")
        indicators = tuple(IndicatorDefinition.from_dict(item) for item in indicator_items)
        indicator_ids = [item.indicator_id for item in indicators]
        if len(set(indicator_ids)) != len(indicator_ids):
            raise DomainError("invalid_interpretation", "indicator ids must be unique")
        conversion_items = body.get("conversions", [])
        if not isinstance(conversion_items, list):
            raise DomainError("invalid_interpretation", "conversions must be a list")
        conversions = tuple(ConversionRule.from_dict(item) for item in conversion_items)
        rule_ids = [rule.rule_id for rule in conversions]
        if len(set(rule_ids)) != len(rule_ids):
            raise DomainError("invalid_interpretation", "conversion rule ids must be unique")
        # 提前构建图以捕获重复路径
        ConversionGraph(conversions)
        mapping_items = body.get("mappings")
        if not isinstance(mapping_items, list) or not mapping_items:
            raise DomainError("invalid_interpretation", "interpretation needs at least one mapping")
        mappings = tuple(MetricMapping.from_dict(item) for item in mapping_items)
        known_indicators = set(indicator_ids)
        seen_keys: set[tuple[str, int, str]] = set()
        for mapping in mappings:
            if mapping.indicator_id not in known_indicators:
                raise DomainError("invalid_mapping",
                                  f"mapping for {mapping.metric} names an unknown indicator")
            for spec_version in mapping.spec_versions:
                # 同一指标在同一规格版本下可按报送单位分别给映射；
                # 同单位的重复映射才是歧义错误。
                key = (mapping.metric, spec_version, mapping.unit)
                if key in seen_keys:
                    raise DomainError("invalid_mapping",
                                      f"metric {mapping.metric} has multiple mappings at spec version "
                                      f"{spec_version} for unit {mapping.unit}")
                seen_keys.add(key)
        return cls(campaign_id, version, indicators, conversions, mappings)

    def to_dict(self) -> dict[str, Any]:
        return {"campaign_id": self.campaign_id, "version": self.version,
                "indicators": [item.to_dict() for item in self.indicators],
                "conversions": [item.to_dict() for item in self.conversions],
                "mappings": [item.to_dict() for item in self.mappings]}

    def indicator(self, indicator_id: str) -> IndicatorDefinition:
        for item in self.indicators:
            if item.indicator_id == indicator_id:
                return item
        raise DomainError("unknown_indicator", f"indicator {indicator_id} is not declared")

    def graph(self) -> ConversionGraph:
        return ConversionGraph(self.conversions)

    def resolve_mapping(self, metric: str, spec_version: int,
                        source_unit: str | None = None) -> MetricMapping | None:
        """按测量时的规格版本（必要时再按报送单位）解析指标身份；找不到即证据不足。"""
        for mapping in self.mappings:
            if mapping.metric != metric or spec_version not in mapping.spec_versions:
                continue
            if source_unit is not None and mapping.unit != source_unit:
                continue
            return mapping
        return None

    def judge(self, metric: str, spec_version: int, source_unit: str,
              target_indicator_id: str) -> tuple[str, dict[str, Any]]:
        """两级判定：先身份、后换算，返回 (结论码, 证据)。

        结论码为 :data:`INCLUDED` 或 :data:`EXCLUSION_REASONS` 之一。
        """
        mapping = self.resolve_mapping(metric, spec_version, source_unit)
        if mapping is None:
            return "no_identity_mapping", {"metric": metric, "spec_version": spec_version,
                                           "source_unit": source_unit}
        if mapping.indicator_id != target_indicator_id or not mapping.equivalent:
            return "definition_changed", {
                "metric": metric, "spec_version": spec_version,
                "mapped_indicator": mapping.indicator_id,
                "target_indicator": target_indicator_id,
                "definition_version": mapping.definition_version,
                "reason": mapping.reason}
        indicator = self.indicator(target_indicator_id)
        path = self.graph().find(source_unit, indicator.unit)
        if path is None:
            return "no_conversion_rule", {
                "metric": metric, "source_unit": source_unit, "target_unit": indicator.unit}
        return INCLUDED, {"mapping": mapping, "indicator": indicator, "path": path}
