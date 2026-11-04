"""在明确隔离的评测样本上进行探索性拒答校准。"""

import math


THRESHOLD_GRID = tuple(round(step / 20, 6) for step in range(21))
RULE_FAMILIES = ("top_score", "top_2_margin", "combined")


def _validated_rows(rows: list[dict]) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Calibration rows must be a non-empty list.")
    checked = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("query_id"), str):
            raise ValueError("Each calibration row needs a query_id.")
        if row["query_id"] in ids:
            raise ValueError("Calibration query IDs must be unique.")
        ids.add(row["query_id"])
        score = row.get("top_score")
        margin = row.get("top_2_margin")
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score)):
            raise ValueError(f"{row['query_id']}: top_score must be finite.")
        if (margin is not None and (isinstance(margin, bool)
                or not isinstance(margin, (int, float)) or not math.isfinite(margin))):
            raise ValueError(f"{row['query_id']}: top_2_margin must be finite or null.")
        if not isinstance(row.get("no_evidence", False), bool):
            raise ValueError(f"{row['query_id']}: no_evidence must be boolean.")
        checked.append({**row, "top_score": float(score),
                        "top_2_margin": None if margin is None else float(margin)})
    return checked


def _validate_threshold(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number from 0 to 1.")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a finite number from 0 to 1.")
    return value


def evaluate_fixed_rule(rows: list[dict], rule: dict) -> dict:
    """评估一条已固定的规则；本函数不会选择或更改规则。"""
    checked = _validated_rows(rows)
    if not isinstance(rule, dict) or rule.get("family") not in RULE_FAMILIES:
        raise ValueError("Rule family must be top_score, top_2_margin or combined.")
    family = rule["family"]
    score_threshold = None
    margin_threshold = None
    if family in {"top_score", "combined"}:
        score_threshold = _validate_threshold(rule.get("top_score_threshold"), "top_score_threshold")
    if family in {"top_2_margin", "combined"}:
        margin_threshold = _validate_threshold(
            rule.get("top_2_margin_threshold"), "top_2_margin_threshold"
        )

    def accepts(row):
        score_ok = score_threshold is None or row["top_score"] >= score_threshold
        margin_ok = (
            margin_threshold is None
            or (row["top_2_margin"] is not None and row["top_2_margin"] >= margin_threshold)
        )
        return score_ok and margin_ok

    positives = [row for row in checked if not row.get("no_evidence", False)]
    negatives = [row for row in checked if row.get("no_evidence", False)]
    accepted_positive = [row for row in positives if accepts(row)]
    accepted_negative = [row for row in negatives if accepts(row)]
    accepted_count = len(accepted_positive) + len(accepted_negative)
    result = {
        "rule": {
            "family": family,
            "top_score_threshold": score_threshold,
            "top_2_margin_threshold": margin_threshold,
        },
        "sample_counts": {
            "total": len(checked), "evidence": len(positives), "no_evidence": len(negatives),
        },
        "coverage": round(accepted_count / len(checked), 6),
        "precision": round(len(accepted_positive) / accepted_count, 6) if accepted_count else None,
        "false_accept_rate": round(len(accepted_negative) / len(negatives), 6) if negatives else None,
        "false_reject_rate": round(
            (len(positives) - len(accepted_positive)) / len(positives), 6
        ) if positives else None,
        "accepted_evidence": len(accepted_positive),
        "accepted_no_evidence": len(accepted_negative),
        "accepted_query_ids": [row["query_id"] for row in checked if accepts(row)],
    }
    return result


def calibrate_abstention(rows: list[dict], thresholds=THRESHOLD_GRID) -> dict:
    """仅使用 Calibration 选择一个探索性零误接受规则。"""
    checked = _validated_rows(rows)
    grid = tuple(_validate_threshold(value, "threshold") for value in thresholds)
    if not grid:
        raise ValueError("At least one calibration threshold is required.")
    evaluations = {"top_score": [], "top_2_margin": [], "combined": []}
    for threshold in grid:
        evaluations["top_score"].append(evaluate_fixed_rule(
            checked, {"family": "top_score", "top_score_threshold": threshold}
        ))
        evaluations["top_2_margin"].append(evaluate_fixed_rule(
            checked, {"family": "top_2_margin", "top_2_margin_threshold": threshold}
        ))
    for score_threshold in grid:
        for margin_threshold in grid:
            evaluations["combined"].append(evaluate_fixed_rule(checked, {
                "family": "combined", "top_score_threshold": score_threshold,
                "top_2_margin_threshold": margin_threshold,
            }))

    candidates = [
        result for family in RULE_FAMILIES for result in evaluations[family]
        if result["false_accept_rate"] == 0.0
    ]
    family_order = {"top_score": 0, "top_2_margin": 1, "combined": 2}
    selected = None
    if candidates:
        selected = min(candidates, key=lambda result: (
            -result["coverage"], result["false_reject_rate"],
            -(result["precision"] or 0), family_order[result["rule"]["family"]],
            result["rule"]["top_score_threshold"] or 0,
            result["rule"]["top_2_margin_threshold"] or 0,
        ))
    return {
        "status": "exploratory_provisional_ai_only",
        "selection_protocol": (
            "Calibration only: require zero false accepts, maximize coverage, then minimize false "
            "rejects; prefer the simpler top-score rule on exact ties. Validation is not consulted."
        ),
        "threshold_grid": list(grid),
        "evaluations": evaluations,
        "zero_false_accept_candidate_count": len(candidates),
        "selected": selected,
    }
