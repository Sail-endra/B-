from __future__ import annotations

import json
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from api.gemini import GeminiClient
from api.grade_predictor import GRADE_SCHEMA, PROMPT, calculate, grade_ladder, solve_item_target, solve_target, validate_schema
from api.models import Source, SourceType
from api.store.sqlite_store import SQLiteStore
from api.syllabus.extract import read_syllabus


def schema():
    return validate_schema({
        "grading_scale": {"letter_grades": [{"letter": "A", "minimum": 93}, {"letter": "B", "minimum": 83}]},
        "components": [
            {"name": "Midterms", "weight": .3, "aggregation": "equal",
             "items": [{"name": f"Midterm {i}", "weight_within_category": None} for i in range(1, 4)], "drop_lowest": 1},
            {"name": "Assignments", "weight": .2, "aggregation": "weighted",
             "items": [{"name": "Essay", "weight_within_category": .6},
                       {"name": "Problem sets", "weight_within_category": .4}], "drop_lowest": 0},
            {"name": "Final", "weight": .5, "aggregation": "equal",
             "items": [{"name": "Final exam", "weight_within_category": None}], "drop_lowest": 0},
        ],
        "special_rules": [], "replacement_rules": [], "extra_credit": [], "uncertainties": [],
    })


def test_actual_grade_drop_current_weight_projection_and_target_solver():
    rules = schema()
    exams = rules["components"][0]["items"]
    assignments = rules["components"][1]["items"]
    final = rules["components"][2]["items"][0]
    actual = {exams[0]["id"]: 87, exams[1]["id"]: 92, exams[2]["id"]: 75,
              assignments[0]["id"]: 95, assignments[1]["id"]: 90}
    current = calculate(rules, actual)
    assert current["current_percent"] == pytest.approx(90.9)
    assert current["completed_weight"] == pytest.approx(.5)
    assert current["remaining_weight"] == pytest.approx(.5)
    assert current["categories"][0]["average"] == pytest.approx(89.5)
    assert current["projected_percent"] is None

    scenario = calculate(rules, actual, {final["id"]: 94})
    assert scenario["projected_percent"] == pytest.approx(92.45)
    target = solve_target(rules, actual, 93)
    assert target["status"] == "achievable"
    assert target["required_average"] == pytest.approx(95.1, abs=.01)
    item_target = solve_item_target(rules, actual, final["id"], 93)
    assert item_target["required_score"] == pytest.approx(95.1, abs=.1)
    assert calculate(rules, actual, {final["id"]: item_target["required_score"]})["projected_percent"] >= 93
    assert calculate(rules, actual, {final["id"]: item_target["required_score"] - .1})["projected_percent"] < 93


def test_grade_ladder_gives_required_average_for_every_syllabus_letter():
    rules = schema()  # letter scale: A>=93, B>=83
    exams = rules["components"][0]["items"]
    assignments = rules["components"][1]["items"]
    # Everything entered except the Final (the one empty field).
    actual = {exams[0]["id"]: 87, exams[1]["id"]: 92, exams[2]["id"]: 75,
              assignments[0]["id"]: 95, assignments[1]["id"]: 90}
    ladder = grade_ladder(rules, actual)
    by_letter = {row["letter"]: row for row in ladder}
    assert [row["letter"] for row in ladder] == ["A", "B"]         # ordered high->low
    # Same target the solver test asserts, now surfaced per-letter.
    assert by_letter["A"]["status"] == "achievable"
    assert by_letter["A"]["required_average"] == pytest.approx(95.1, abs=.05)
    assert by_letter["B"]["required_average"] == pytest.approx(75.1, abs=.05)
    # And the ladder reaching a grade must actually clear its threshold.
    need_a = by_letter["A"]["required_average"]
    assert calculate(rules, actual, {rules["components"][2]["items"][0]["id"]: need_a})["projected_percent"] >= 93


def test_item_target_holds_entered_what_if_scores_and_models_remaining_blank_scores():
    rules = schema()
    ids = {item["name"]: item["id"] for component in rules["components"] for item in component["items"]}
    actual = {ids["Midterm 1"]: 90, ids["Midterm 2"]: 90, ids["Midterm 3"]: 90}
    # An expected essay grade is treated as fixed; the unentered problem set
    # follows the requested same-score assumption as the selected final.
    target = solve_item_target(rules, actual, ids["Final exam"], 83,
                               hypothetical={ids["Essay"]: 90},
                               unfilled_assumption="same_score")
    assert target["status"] == "achievable"
    assert 0 <= target["required_score"] <= 100
    optimistic = solve_item_target(rules, actual, ids["Final exam"], 93,
                                   hypothetical={ids["Essay"]: 90},
                                   unfilled_assumption="hundred")
    conservative = solve_item_target(rules, actual, ids["Final exam"], 93,
                                     hypothetical={ids["Essay"]: 90},
                                     unfilled_assumption="zero")
    assert optimistic["required_score"] <= conservative.get("required_score", 100)


def test_drop_rule_waits_for_all_scores_and_solver_handles_impossible_targets():
    rules = schema()
    exams = rules["components"][0]["items"]
    actual = {exams[0]["id"]: 87, exams[1]["id"]: 92}
    partial = calculate(rules, actual)
    assert partial["categories"][0]["average"] == pytest.approx(89.5)
    assert partial["categories"][0]["completed_weight"] == pytest.approx(.2)
    assert solve_target(rules, actual, 99)["status"] == "impossible"


def test_replacement_rule_is_applied_by_deterministic_engine():
    raw = {
        "grading_scale": {"letter_grades": []}, "special_rules": [], "extra_credit": [], "uncertainties": [],
        "replacement_rules": [{"source_item_name": "Final exam", "target_category_name": "Midterms", "replace_if_higher": True}],
        "components": [
            {"name": "Midterms", "weight": .3, "aggregation": "equal", "drop_lowest": 0,
             "items": [{"name": f"Midterm {i}", "weight_within_category": None} for i in range(1, 4)]},
            {"name": "Final", "weight": .4, "aggregation": "equal", "drop_lowest": 0,
             "items": [{"name": "Final exam", "weight_within_category": None}]},
            {"name": "Homework", "weight": .3, "aggregation": "equal", "drop_lowest": 0,
             "items": [{"name": "Homework average", "weight_within_category": None}]},
        ],
    }
    rules = validate_schema(raw)
    ids = {item["name"]: item["id"] for c in rules["components"] for item in c["items"]}
    result = calculate(rules, {ids["Midterm 1"]: 60, ids["Midterm 2"]: 90,
                               ids["Midterm 3"]: 80, ids["Final exam"]: 95,
                               ids["Homework average"]: 100})
    assert result["projected_percent"] == pytest.approx(94.5)


def test_extra_credit_is_additive_and_not_part_of_normal_weight_or_required_average():
    raw = schema()
    raw["extra_credit"] = [{"name": "Optional reflection", "description": "Optional bonus",
                            "maximum_percentage_points": 2}]
    rules = validate_schema(raw)
    all_scores = {item["id"]: 92 for component in rules["components"] for item in component["items"]}
    extra = rules["extra_credit"][0]
    baseline = calculate(rules, all_scores)
    assert baseline["completed_weight"] == pytest.approx(1)
    assert baseline["projected_percent"] == pytest.approx(92)
    with_bonus = calculate(rules, all_scores, {extra["id"]: 50})
    assert with_bonus["projected_percent"] == pytest.approx(93)
    target = solve_target(rules, all_scores, 93)
    assert target["status"] == "extra_credit_only"
    assert target["required_average"] == pytest.approx(50, abs=.01)


def test_schema_validation_rejects_invalid_weights_and_duplicate_fields():
    bad = schema()
    bad["components"][0]["weight"] = 3
    with pytest.raises(ValueError):
        validate_schema(bad)


def test_database_keeps_extraction_confirmed_rules_and_scores_separate(tmp_path):
    store = SQLiteStore(tmp_path / "grades.sqlite")
    try:
        course_schema = schema()
        extraction = store.save_grade_extraction("user", "course", "sha", course_schema, "gemini:test")
        assert store.grade_predictor("user", "course")["confirmed"] is None
        store.confirm_grade_rules("user", "course", course_schema, extraction)
        item_id = course_schema["components"][0]["items"][0]["id"]
        store.set_grade_score("user", "course", item_id, 87.5)
        second = store.save_grade_extraction("user", "course", "new-sha", course_schema, "gemini:test")
        result = store.grade_predictor("user", "course")
        assert result["pending"]["id"] == second
        assert result["confirmed"] is not None
        assert result["scores"][item_id] == 87.5
    finally:
        store.close()


def test_existing_course_syllabus_is_discovered_analyzed_once_and_remains_course_scoped(tmp_path, monkeypatch):
    from api import main
    project = Path(__file__).parents[1]
    uploads = project / "data" / "uploads"
    econ_dir = uploads / "local-user" / "econ303"
    real_file = econ_dir / "_syllabus_econ303.pdf"
    wrong_file = econ_dir / "syllabus.pdf"
    if not real_file.exists():
        pytest.skip("existing ECON 303 syllabus is unavailable in this checkout")
    store = SQLiteStore(tmp_path / "existing-syllabus.sqlite")
    store.upsert_course_stub("local-user", "econ303", "ECON 303", "ECON 303")
    # Existing uploads may be generic readings records or old disk-only syllabus files.
    if wrong_file.exists():
        store.upsert_source(Source("econ303_wrong", "local-user", "econ303", "syllabus",
                                   SourceType.READINGS, str(wrong_file)))
    monkeypatch.setattr(main, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "local-user")
    whole_text = read_syllabus(real_file).strip()
    calls = []

    class FakeGemini:
        backend = "gemini:test"
        def complete(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            assert whole_text in prompt
            assert prompt.endswith("FULL SYLLABUS END")
            return json.dumps(schema())

    monkeypatch.setattr(main, "_grade_llm", lambda _store, _user: FakeGemini())
    try:
        detected = main.get_grade_syllabus("econ303")
        assert detected["exists"] is True
        assert len(detected["candidates"]) == 1  # duplicate legacy PDF deduplicated; wrong-course PDF rejected
        assert detected["selected"]["file_name"] == real_file.name
        analyzed = main.analyze_grade_syllabus("econ303", main.GradeSyllabusAnalyzeRequest())
        assert analyzed["status"] == "pending_review"
        assert len(calls) == 1
        # Opening again finds the same stored extraction and does not call Gemini.
        again = main.get_grade_syllabus("econ303")
        assert again["selected"]["status"] == "pending"
        main.analyze_grade_syllabus("econ303", main.GradeSyllabusAnalyzeRequest())
        assert len(calls) == 1
        extraction_id = analyzed["extraction"]["id"]
        main.confirm_grade_predictor("econ303", main.GradeRulesRequest(
            schema=analyzed["extraction"]["schema"], extraction_id=extraction_id))
        rules = analyzed["extraction"]["schema"]
        item_ids = [item["id"] for component in rules["components"] for item in component["items"]]
        main.put_grade("econ303", item_ids[0], main.GradeScoreRequest(score=90))
        main.put_grade("econ303", item_ids[1], main.GradeScoreRequest(score=80))
        saved = main.get_grade_predictor("econ303")
        assert saved["scores"][item_ids[0]] == 90
        assert saved["scores"][item_ids[1]] == 80
        assert saved["calculation"]["current_percent"] is not None
        assert main.post_grade_calculation("econ303", main.GradeCalculateRequest(target_percent=90))["target"]
        # Re-analysis makes a new proposal while retaining entered grades.
        main.analyze_grade_syllabus("econ303", main.GradeSyllabusAnalyzeRequest(force=True))
        assert len(calls) == 2
        assert main.get_grade_predictor("econ303")["scores"] == saved["scores"]
        with pytest.raises(Exception):
            main.get_grade_syllabus("csci303")  # this fixture has no CSCI 303 course
    finally:
        store.close()


def test_multiple_syllabus_candidates_require_an_explicit_course_scoped_choice(tmp_path, monkeypatch):
    from api import main
    store = SQLiteStore(tmp_path / "multiple-syllabi.sqlite")
    root = tmp_path / "uploads"
    course_dir = root / "student" / "econ304"
    course_dir.mkdir(parents=True)
    (course_dir / "ECON 304 Syllabus.txt").write_text(
        "ECON 304 Syllabus\nCourse description and grading: exams count 50 percent.")
    (course_dir / "ECON 304 Course Outline.txt").write_text(
        "ECON 304 Course Outline\nAssessment and grading: projects count 50 percent.")
    (root / "student" / "econ315").mkdir(parents=True)
    (root / "student" / "econ315" / "ECON 315 Syllabus.txt").write_text(
        "ECON 315 Syllabus\nCourse description and grading: exams count 100 percent.")
    store.upsert_course_stub("student", "econ304", "ECON 304", "ECON 304")
    store.upsert_course_stub("student", "econ315", "ECON 315", "ECON 315")
    monkeypatch.setattr(main, "UPLOAD_DIR", root)
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "student")
    try:
        found = main.get_grade_syllabus("econ304")
        assert found["ambiguous"] is True
        assert len(found["candidates"]) == 2
        with pytest.raises(Exception):
            main.analyze_grade_syllabus("econ304", main.GradeSyllabusAnalyzeRequest())
        wrong = main.get_grade_syllabus("econ315")
        assert [row["file_name"] for row in wrong["candidates"]] == ["ECON 315 Syllabus.txt"]
        main.select_grade_syllabus("econ304", main.GradeSyllabusSelectionRequest(
            source_id=found["candidates"][0]["source_id"]))
        assert store.grade_syllabus_selection("student", "econ304")["file_name"] == found["candidates"][0]["file_name"]
    finally:
        store.close()


def test_grade_predictor_upload_registers_file_and_analyzes_through_existing_flow(tmp_path, monkeypatch):
    from api import main
    store = SQLiteStore(tmp_path / "syllabus-upload.sqlite")
    store.upsert_course_stub("student", "newcourse", "NEW 100", "New Course")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "student")
    monkeypatch.setattr(main, "double_extract", lambda *_args, **_kwargs: SimpleNamespace(
        extraction=object(), backend="mock", conflicts=[], agreement_rate=1.0))
    monkeypatch.setattr(main, "build_linker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "commit_syllabus", lambda *_args, **_kwargs: {
        "rows": 0, "linked": 0, "needs_review": 0})
    class FakeGemini:
        backend = "gemini:test"
        def complete(self, *_args, **_kwargs): return json.dumps(schema())
    monkeypatch.setattr(main, "_grade_llm", lambda *_args: FakeGemini())
    upload = UploadFile(filename="New Course Syllabus.txt", file=io.BytesIO(
        b"New Course syllabus. Grading includes exams and assignments."))
    try:
        result = asyncio.run(main.upload_syllabus("newcourse", upload))
        assert result["grade_extraction"]["status"] == "pending_review"
        registered = store.sources("student", "newcourse")
        assert len(registered) == 1
        assert registered[0].type == SourceType.SYLLABUS
        assert Path(registered[0].file).exists()
        assert main.get_grade_syllabus("newcourse")["selected"]["file_name"] == "New Course Syllabus.txt"
    finally:
        store.close()


def test_offline_mode_returns_an_actionable_grade_analysis_error(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from api import main
    store = SQLiteStore(tmp_path / "offline-grade.sqlite")
    store.upsert_course_stub("student", "course", "COURSE 101", "Course")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "settings", SimpleNamespace(offline=True, gemini_api_key="configured"))
    try:
        with pytest.raises(HTTPException) as error:
            main._analyze_grade_syllabus("student", "course", {
                "source_id": "course_syllabus", "file_name": "Syllabus.pdf",
                "hash": "content-hash", "text": "Complete syllabus text",
            })
        assert error.value.status_code == 503
        assert "offline mode" in error.value.detail
        assert "COPILOT_OFFLINE=0" in error.value.detail
    finally:
        store.close()


def test_grade_api_workflow_confirms_saves_actual_and_keeps_what_if_temporary(tmp_path, monkeypatch):
    from api import main

    store = SQLiteStore(tmp_path / "grade-api.sqlite")
    store.upsert_course_stub("student", "course", "COURSE", "Course")
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main, "current_user", lambda: "student")
    try:
        rules = schema()
        main.confirm_grade_predictor("course", main.GradeRulesRequest(schema=rules))
        first = rules["components"][0]["items"][0]["id"]
        main.put_grade("course", first, main.GradeScoreRequest(score=87.5))
        hypothetical = rules["components"][2]["items"][0]["id"]
        result = main.post_grade_calculation("course", main.GradeCalculateRequest(
            hypothetical={hypothetical: 94}, target_percent=93))
        assert result["calculation"]["projected_percent"] is None
        assert result["target"]["status"] == "achievable"
        field_result = main.post_grade_calculation("course", main.GradeCalculateRequest(
            hypothetical={hypothetical: 94}, target_item_id=hypothetical,
            unfilled_assumption="same_score"))
        assert [(row["letter"], row["minimum"]) for row in field_result["field_targets"]] == [
            ("A", 93), ("B", 83)]
        assert all(row["status"] in {"achievable", "impossible", "guaranteed"}
                   for row in field_result["field_targets"])
        saved = main.get_grade_predictor("course")
        assert saved["scores"] == {first: 87.5}
        assert hypothetical not in saved["scores"]
    finally:
        store.close()


def test_real_syllabus_text_is_sent_whole_with_structured_output(monkeypatch):
    path = Path(__file__).parents[1] / "materials" / "coll150" / "_syllabus.docx"
    if not path.exists():
        pytest.skip("bundled real syllabus is unavailable")
    whole_text = read_syllabus(path)
    assert len(whole_text) > 12_000

    captured = {}
    client = GeminiClient("not-a-real-key", model="gemini-2.5-flash-lite")

    def mocked_call(prompt, system, max_tokens, temperature, response_schema=None):
        captured.update(prompt=prompt, schema=response_schema, max_tokens=max_tokens, temperature=temperature)
        return json.dumps({
            "grading_scale": {"letter_grades": []},
            "components": [
                {"name": "Class Attendance, In-Class Writing, Discussion, Tutorial", "weight": .2,
                 "aggregation": "equal", "items": [{"name": "Attendance and class work", "weight_within_category": None}], "drop_lowest": 0, "drop_highest": 0},
                {"name": "Four Short Essays", "weight": .8, "aggregation": "equal",
                 "items": [{"name": f"Essay {i}", "weight_within_category": None} for i in range(1, 5)], "drop_lowest": 1, "drop_highest": 0},
            ],
            "special_rules": [], "replacement_rules": [], "extra_credit": [], "uncertainties": [],
        })

    monkeypatch.setattr(client, "_call", mocked_call)
    raw = client.complete(PROMPT + whole_text + "\nFULL SYLLABUS END", max_tokens=8000,
                          temperature=0, response_schema=GRADE_SCHEMA, cache=False)
    assert len(captured["prompt"]) > len(whole_text)
    assert whole_text[-100:] in captured["prompt"]
    assert captured["schema"] == GRADE_SCHEMA
    extracted = validate_schema(json.loads(raw))
    assert [category["weight"] for category in extracted["components"]] == [.2, .8]
    assert extracted["components"][1]["drop_lowest"] == 1
    ids = {item["name"]: item["id"] for category in extracted["components"] for item in category["items"]}
    grades = {ids["Attendance and class work"]: 95, ids["Essay 1"]: 85,
              ids["Essay 2"]: 90, ids["Essay 3"]: 75}
    current = calculate(extracted, grades)
    assert current["current_percent"] == pytest.approx(86.25)
    assert solve_target(extracted, grades, 90)["required_average"] == pytest.approx(91.25, abs=.01)
