"""Compare question strategies with fixed simulated users and hidden facts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))

from taxharness.question_planner import rank_questions
from taxharness.question_simulator import QuestionSimulator


FACTS = {
    "F002": {"purpose": "购买培训资料"},
    "F003": {"purpose": "寄送客户验收文件"},
    "F006": {"purpose": "研发项目管理服务"},
    "F009": {"purpose": "维护生产设备"},
}


def _question(question_id: str, targets: list[str], burden: int) -> dict:
    return {"id": question_id, "text": f"请说明 {'、'.join(targets)} 的实际用途。", "targets": targets,
            "required_slots": {target: ["purpose"] for target in targets}, "burden": burden, "priority": 5}


def _run(case: dict, policy: str) -> dict:
    targets = case["targets"]
    singles = [_question(f"one-{target}", [target], 1) for target in targets]
    group = _question("group", targets, max(2, len(targets) - 1))
    candidates = singles if policy == "individual" else [group, *singles]
    simulator = QuestionSimulator({target: FACTS[target] for target in targets}, response_plan=case.get("responses", {}))
    known = {(target, "purpose") for target in case.get("known", [])}
    waiting = set(case.get("waiting", []))
    asked = set()
    turns = []
    while True:
        remaining = [candidate for candidate in candidates if candidate["id"] not in asked]
        ranked = rank_questions(remaining, known_slots=known, deferred_targets=waiting,
                                simulate_responses=policy == "forecast")
        if not ranked:
            break
        question = ranked[0]
        answer = simulator.answer(question)
        turns.append({"question": question["id"], "coverage": question["coverage_slots"],
                      "answer_status": answer["status"], "answer_length": len(answer["text"]),
                      "burden": question["burden"]})
        asked.add(question["id"])
        known.update((item["target_id"], item["slot"]) for item in answer["provided_slots"])
        waiting.update(answer["deferred_targets"])
    return {"case": case["name"], "policy": policy, "rounds": len(turns),
            "answered": sorted(target for target, slot in known if slot == "purpose"),
            "waiting": sorted(waiting), "unresolved": sorted(set(targets) - waiting - {target for target, _ in known}),
            "answer_characters": sum(turn["answer_length"] for turn in turns),
            "burden_points": sum(turn["burden"] for turn in turns), "turns": turns}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = [
        {"name": "三票均可回答", "targets": ["F002", "F003", "F006"]},
        {"name": "部分回答并暂缓", "targets": ["F002", "F003", "F009"], "responses": {
            "group": {"mode": "partial", "provide": [("F002", "purpose")], "defer_targets": ["F009"]},
            "one-F009": "defer"}},
        {"name": "已有一项草稿", "targets": ["F002", "F003", "F006"], "known": ["F002"]},
        {"name": "已有一项暂缓", "targets": ["F002", "F003", "F009"], "waiting": ["F009"]},
        {"name": "四票回答不完整", "targets": ["F002", "F003", "F006", "F009"], "responses": {
            "group": {"mode": "partial", "provide": [("F002", "purpose"), ("F003", "purpose")], "defer_targets": ["F009"]},
            "one-F009": "defer"}},
    ]
    rows = [_run(case, policy) for case in cases for policy in ("individual", "ranked", "forecast")]
    summary = {policy: {"rounds": sum(row["rounds"] for row in rows if row["policy"] == policy),
                        "unresolved": sum(len(row["unresolved"]) for row in rows if row["policy"] == policy),
                        "burden_points": sum(row["burden_points"] for row in rows if row["policy"] == policy)}
               for policy in ("individual", "ranked", "forecast")}
    result = {"description": "固定假设用户的离线策略比较；回答分支概率是启发值，非真实用户统计。",
              "summary": summary, "cases": rows}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
