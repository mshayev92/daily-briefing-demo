"""Regression tests for orchestrator._normalize_course()."""

import orchestrator


KNOWN_COURSES = {
    "ENGL001": {"class": "engl"},
    "ECON001": {"class": "econ"},
    "MATH001": {"class": "math"},
    "PHIL001": {"class": "phil"},
    "CMSC001": {"class": "cmsc"},
    "CS-Advising": {"class": "advising"},
}

STATE = {
    "courses": KNOWN_COURSES,
    "learned_patterns": {
        "canvas_nicknames": {
            "CMSC001": "CS",
            "ECON001": "Econ",
            "ENGL001": "English",
            "MATH001": "Math",
            "PHIL001": "AI & Ethics",
        }
    },
}


def test_normalize_course_accepts_spaced_canonical_codes():
    assert orchestrator._normalize_course("econ 001", STATE) == "ECON001"
    assert orchestrator._normalize_course("MATH 001", STATE) == "MATH001"


def test_normalize_course_preserves_existing_nickname_and_advising_behavior():
    assert orchestrator._normalize_course("Math", STATE) == "MATH001"
    assert orchestrator._normalize_course("AI & Ethics", STATE) == "PHIL001"
    assert orchestrator._normalize_course("CS", STATE) == "CMSC001"
    assert orchestrator._normalize_course("CS", STATE, "advising") == "CS-Advising"


def test_normalize_course_leaves_unknown_value_unchanged():
    assert orchestrator._normalize_course("nonsense", STATE) == "nonsense"
