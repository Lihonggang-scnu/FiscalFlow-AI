"""Offline demonstration of grouped follow-up planning and simulated replies."""

from __future__ import annotations

import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from taxharness.question_planner import rank_questions
from taxharness.question_simulator import QuestionSimulator


FACTS = {
    "office": {"purpose": "日常办公用品"},
    "meeting": {"purpose": "研讨会餐费"},
    "subscription": {"purpose": "云端档案服务"},
}
LABELS = {"office": "办公用品票据", "meeting": "会务票据", "subscription": "订阅服务票据"}


def question(question_id, text, targets, *, burden=1, priority=5):
    return {
        "id": question_id,
        "text": text,
        "targets": list(targets),
        "required_slots": {target: ["purpose"] for target in targets},
        "burden": burden,
        "priority": priority,
    }


def run_plan(candidates, simulator):
    known_slots = set()
    deferred_targets = set()
    recently_asked = set()
    turns = []
    while ranked := rank_questions(
        candidates,
        known_slots=known_slots,
        deferred_targets=deferred_targets,
        recently_asked=recently_asked,
    ):
        selected = ranked[0]
        response = simulator.answer(selected)
        turns.append((selected, response))
        known_slots.update((item["target_id"], item["slot"]) for item in response["provided_slots"])
        deferred_targets.update(response["deferred_targets"])
        recently_asked.add(selected["id"])
    return turns, known_slots, deferred_targets


def main():
    batch = question(
        "batch",
        "办公、会务和订阅票据的用途分别是什么？能确认的先告诉我。",
        list(FACTS),
        burden=2,
    )
    meeting = question("meeting", "会务票据的用途是什么？", ["meeting"])
    group_simulator = QuestionSimulator(
        FACTS,
        labels=LABELS,
        response_plan={
            "batch": {
                "mode": "partial",
                "provide": [("office", "purpose")],
                "defer_targets": ["subscription"],
            }
        },
    )
    grouped_turns, grouped_known, grouped_waiting = run_plan([batch, meeting], group_simulator)

    individual_questions = [
        question(f"item_{target}", f"{LABELS[target]}的用途是什么？", [target])
        for target in FACTS
    ]
    individual_simulator = QuestionSimulator(
        FACTS,
        labels=LABELS,
        response_plan={"item_subscription": "defer"},
    )
    individual_turns, individual_known, individual_waiting = run_plan(individual_questions, individual_simulator)

    print("主动问询模拟（纯离线）")
    for name, turns, known, waiting in (
        ("批量优先", grouped_turns, grouped_known, grouped_waiting),
        ("逐项提问", individual_turns, individual_known, individual_waiting),
    ):
        print(f"\n{name}：{len(turns)} 轮")
        for selected, response in turns:
            print(f"  问：{selected['text']}")
            print(f"  答：{response['text']}")
        completed = sorted({target for target, slot in known if slot == "purpose"})
        completed_labels = [LABELS.get(target, target) for target in completed]
        waiting_labels = [LABELS.get(target, target) for target in sorted(waiting)]
        print(f"  已补充对象：{'、'.join(completed_labels) or '无'}；等待对象：{'、'.join(waiting_labels) or '无'}")

    correction_simulator = QuestionSimulator(
        {"meeting": FACTS["meeting"]},
        labels=LABELS,
        response_plan={
            "initial": {"mode": "complete", "values": {"meeting": {"purpose": "会议茶歇"}}},
            "correction": "correction",
        },
    )
    original = question("initial", "会务票据的用途是什么？", ["meeting"])
    correction = question("correction", "请补充更正后的用途。", ["meeting"])
    correction_simulator.answer(original)
    correction_reply = correction_simulator.answer(correction)
    print(f"\n更正示例：{correction_reply['text']}")
    print(f"轮次对比：批量 {len(grouped_turns)} 轮，逐项 {len(individual_turns)} 轮。")
    print(f"等待项对比：批量 {[LABELS[target] for target in sorted(grouped_waiting)]}；逐项 {[LABELS[target] for target in sorted(individual_waiting)]}。")


if __name__ == "__main__":
    main()
