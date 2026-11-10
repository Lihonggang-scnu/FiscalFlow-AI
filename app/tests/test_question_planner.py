from __future__ import annotations

from taxharness.question_planner import rank_questions
from taxharness.question_simulator import QuestionSimulator


def _question(question_id, text, targets, *, slots=None, burden=1, priority=5):
    slots = slots or {target: ["purpose"] for target in targets}
    return {
        "id": question_id,
        "text": text,
        "targets": list(targets),
        "required_slots": slots,
        "burden": burden,
        "priority": priority,
    }


def test_grouped_question_ranks_over_single_questions_and_keeps_chinese_text():
    candidates = [
        _question("one", "办公用品票据的用途是什么？", ["office"]),
        _question("group", "办公用品和会议服务分别用于什么？", ["office", "meeting"], burden=2),
        _question("two", "会议服务票据的用途是什么？", ["meeting"]),
    ]

    ranked = rank_questions(candidates)

    assert ranked[0]["id"] == "group"
    assert ranked[0]["text"] == "办公用品和会议服务分别用于什么？"
    assert ranked[0]["scores"]["coverage"] == 1.0
    assert set(ranked[0]["scores"]) == {"coverage", "redundancy", "burden", "waiting", "cooldown", "lookahead"}
    assert ranked[0]["lookahead_slots"] == 0
    assert ranked[0]["rank"] == 1


def test_known_slots_and_waiting_targets_remove_unneeded_questions():
    candidates = [
        _question("known", "再说一次已确认的用途？", ["office"]),
        _question("waiting", "订阅服务的用途是什么？", ["subscription"]),
        _question("mixed", "办公、快递和订阅票据分别是什么用途？", ["office", "courier", "subscription"]),
    ]

    ranked = rank_questions(
        candidates,
        known_slots={("office", "purpose")},
        deferred_targets={"subscription"},
    )

    assert [item["id"] for item in ranked] == ["mixed"]
    assert ranked[0]["coverage_slots"] == [{"target_id": "courier", "slot": "purpose"}]
    assert ranked[0]["redundant_slots"] == [{"target_id": "office", "slot": "purpose"}]
    assert ranked[0]["waiting_targets"] == ["subscription"]
    assert ranked[0]["scores"]["redundancy"] == 0.5
    assert ranked[0]["scores"]["waiting"] == round(2 / 3, 4)
    assert rank_questions(candidates[:2], known_slots={("office", "purpose")}, deferred_targets={"subscription"}) == []
    assert rank_questions([]) == []


def test_one_step_lookahead_counts_new_coverage_from_the_next_question():
    candidates = [
        _question("a", "先问办公项目？", ["office"]),
        _question("ab", "办公和会务用途分别是什么？", ["office", "meeting"]),
        _question("bc", "会务和订阅用途分别是什么？", ["meeting", "subscription"]),
    ]

    ranked = {item["id"]: item for item in rank_questions(candidates)}

    assert ranked["a"]["lookahead_slots"] == 2
    assert ranked["a"]["next_question_id"] == "bc"
    assert ranked["a"]["scores"]["lookahead"] == 1.0
    assert ranked["ab"]["lookahead_slots"] == 1


def test_response_forecast_compares_hypotheses_without_inventing_answers():
    candidates = [
        _question("batch", "两张票分别用于什么？", ["office", "meeting"], burden=2),
        _question("single", "办公票用于什么？", ["office"]),
    ]
    ranked = {item["id"]: item for item in rank_questions(candidates, simulate_responses=True)}
    forecast = ranked["batch"]["forecast"]
    assert forecast["hypothetical"] is True
    assert [branch["answer"] for branch in forecast["branches"]] == ["complete", "partial", "unknown"]
    assert sum(branch["probability"] for branch in forecast["branches"]) == 1.0
    assert forecast["branches"][0]["expected_new_slots"] == 2
    assert forecast["branches"][-1]["expected_new_slots"] == 0
    assert all("value" not in branch and "text" not in branch for branch in forecast["branches"])


def test_cooldown_and_partial_answer_update_the_next_choice():
    candidates = [
        _question("batch", "两项票据的用途分别是什么？", ["office", "meeting"], burden=2),
        _question("office", "办公用品票据的用途是什么？", ["office"]),
        _question("meeting", "会务票据的用途是什么？", ["meeting"]),
    ]
    simulator = QuestionSimulator(
        {"office": {"purpose": "办公耗材"}, "meeting": {"purpose": "研讨会餐费"}},
        response_plan={"batch": {"mode": "partial", "provide": [("office", "purpose")]}},
    )

    first = rank_questions(candidates)[0]
    answer = simulator.answer(first)
    known = {(item["target_id"], item["slot"]) for item in answer["provided_slots"]}
    next_questions = rank_questions(candidates, known_slots=known, recently_asked={first["id"]})

    assert answer["status"] == "partial"
    assert answer["provided_values"] == [{"target_id": "office", "slot": "purpose", "value": "办公耗材"}]
    assert next_questions[0]["id"] == "meeting"
    assert next_questions[0]["scores"]["cooldown"] == 1.0
    assert all(item["id"] != "office" for item in next_questions)


def test_simulated_batch_completes_answerable_objects_and_keeps_deferred_waiting():
    candidates = [
        _question("batch", "能否分别说明办公、会务和订阅票据的用途？", ["office", "meeting", "subscription"], burden=2),
        _question("meeting", "会务票据的用途是什么？", ["meeting"]),
    ]
    simulator = QuestionSimulator(
        {
            "office": {"purpose": "日常办公用品"},
            "meeting": {"purpose": "研讨会餐费"},
            "subscription": {"purpose": "云端档案服务"},
        },
        labels={"office": "办公用品票据", "meeting": "会务票据", "subscription": "订阅服务票据"},
        response_plan={
            "batch": {
                "mode": "partial",
                "provide": [("office", "purpose")],
                "defer_targets": ["subscription"],
            },
            "meeting": "complete",
        },
    )
    known_slots = set()
    deferred_targets = set()
    recently_asked = set()
    rounds = []

    while ranked := rank_questions(
        candidates,
        known_slots=known_slots,
        deferred_targets=deferred_targets,
        recently_asked=recently_asked,
    ):
        chosen = ranked[0]
        reply = simulator.answer(chosen)
        rounds.append(reply)
        known_slots.update((item["target_id"], item["slot"]) for item in reply["provided_slots"])
        deferred_targets.update(reply["deferred_targets"])
        recently_asked.add(chosen["id"])

    assert len(rounds) == 2
    assert {target for target, slot in known_slots if slot == "purpose"} == {"office", "meeting"}
    assert deferred_targets == {"subscription"}
    assert "日常办公用品" in rounds[0]["text"]
    assert "暂时还不能确认" in rounds[0]["text"]
    assert "研讨会餐费" in rounds[1]["text"]
    assert rank_questions(candidates, known_slots=known_slots, deferred_targets=deferred_targets) == []


def test_correction_is_natural_and_reports_the_replacement_value():
    simulator = QuestionSimulator(
        {"meeting": {"purpose": "研讨会餐费"}},
        labels={"meeting": "会务票据"},
        response_plan={
            "initial": {"mode": "complete", "values": {"meeting": {"purpose": "会议茶歇"}}},
            "correction": "correction",
        },
    )
    original = _question("initial", "会务票据的用途是什么？", ["meeting"])
    correction = _question("correction", "请补充更正后的用途。", ["meeting"])

    first = simulator.answer(original)
    corrected = simulator.answer(correction)

    assert first["provided_values"][0]["value"] == "会议茶歇"
    assert corrected["text"] == "更正一下，会务票据的用途应该是研讨会餐费。"
    assert corrected["corrections"] == [{
        "target_id": "meeting",
        "slot": "purpose",
        "previous_value": "会议茶歇",
        "value": "研讨会餐费",
    }]


def test_bulk_question_uses_fewer_rounds_than_one_question_per_object():
    facts = {
        "office": {"purpose": "日常办公用品"},
        "meeting": {"purpose": "研讨会餐费"},
        "subscription": {"purpose": "云端档案服务"},
    }
    batch_question = _question("batch", "请分别说明这些票据的用途。", list(facts), burden=2)
    batch_simulator = QuestionSimulator(
        facts,
        response_plan={"batch": {"mode": "partial", "provide": [("office", "purpose"), ("meeting", "purpose")], "defer_targets": ["subscription"]}},
    )
    batch_rounds = [batch_simulator.answer(batch_question)]

    individual_questions = [
        _question(f"item_{target}", f"{target}票据的用途是什么？", [target])
        for target in facts
    ]
    individual_simulator = QuestionSimulator(
        facts,
        response_plan={"item_subscription": {"mode": "defer"}},
    )
    individual_rounds = [individual_simulator.answer(question) for question in individual_questions]

    assert len(batch_rounds) == 1
    assert len(individual_rounds) == 3
    assert {item["target_id"] for item in batch_rounds[0]["provided_slots"]} == {"office", "meeting"}
    assert batch_rounds[0]["deferred_targets"] == ["subscription"]

