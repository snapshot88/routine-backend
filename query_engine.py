"""
Deterministic query engine for the CSE routine knowledge base.

Design principle: a tiny LLM is bad at multi-step counting and date-based
rotation math over a table. So none of that logic lives in the LLM prompt.
Instead this module answers every "tricky" question class with plain code
against kb.json, and the LLM's only job (see llm_router.py) is to (a) map a
free-text question to one of these function calls, and (b) turn the
structured result back into a natural sentence.
"""

import json
import datetime
from pathlib import Path
from collections import defaultdict

KB_PATH = Path(__file__).parent / "kb.json"


def load_kb():
    with open(KB_PATH) as f:
        return json.load(f)


KB = load_kb()
TERM_START = datetime.date.fromisoformat(KB["meta"]["class_starting_date"])  # a Sunday


# ---------------------------------------------------------------------------
# Rotation math
# ---------------------------------------------------------------------------

def week_number(on_date: datetime.date) -> int:
    """Week 1 = the week containing the term start date. Weeks run Sun-Thu here."""
    delta_days = (on_date - TERM_START).days
    return (delta_days // 7) + 1


def is_odd_week(on_date: datetime.date = None) -> bool:
    on_date = on_date or datetime.date.today()
    return week_number(on_date) % 2 == 1


def resolve_rotation(session: dict, on_date: datetime.date = None) -> str:
    """Given a rotating lab session, return the subgroup that actually attends
    on the given date, e.g. 'A1' or 'A2'."""
    if not session.get("rotates"):
        return session.get("subgroup")
    odd = is_odd_week(on_date)
    pattern = session["rotation_pattern"]  # e.g. "A1_odd_A2_even"
    first_group, _, second_group = pattern.split("_")[0], None, pattern.split("_")[2]
    return first_group if odd else second_group


def lab_rotation_today(section: str, on_date: datetime.date = None):
    """For a section, tell you exactly which subgroup is doing which lab this week."""
    on_date = on_date or datetime.date.today()
    wk = week_number(on_date)
    out = []
    for s in KB["sessions"]:
        if s["section"] == section and s["type"] == "lab" and s.get("rotates"):
            grp = resolve_rotation(s, on_date)
            out.append({
                "day": s["day"], "periods": s["periods"], "course": s["course"],
                "attending_subgroup": grp, "week_number": wk,
                "week_parity": "odd" if wk % 2 == 1 else "even"
            })
    return out


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def sessions_for(section=None, day=None, course=None, subgroup=None):
    res = []
    for s in KB["sessions"]:
        if section and s["section"] != section:
            continue
        if day and s["day"].lower() != day.lower():
            continue
        if course and s["course"] != course.upper():
            continue
        if subgroup and s.get("subgroup") not in (subgroup, "rotates"):
            continue
        res.append(s)
    return res


def day_schedule(section: str, day: str):
    rows = sorted(sessions_for(section=section, day=day), key=lambda s: s["periods"][0])
    out = []
    for s in rows:
        c = KB["courses"][s["course"]]
        out.append({
            "periods": s["periods"],
            "course": s["course"],
            "title": c["title"],
            "type": s["type"],
            "teachers": [KB["teachers"].get(t, t) for t in s["teachers"]],
            "subgroup": "rotates (alternates weekly)" if s.get("rotates") else s.get("subgroup"),
            "common_slot": s.get("common", False),
        })
    return out


def teacher_schedule(teacher_code: str):
    teacher_code = teacher_code.upper()
    hits = [s for s in KB["sessions"] if teacher_code in s.get("teachers", [])]
    return sorted(hits, key=lambda s: (s["day"], s["periods"][0]))


# ---------------------------------------------------------------------------
# Study-hour / contact-hour questions
# ---------------------------------------------------------------------------

def weekly_contact_minutes(course_code: str, section: str) -> dict:
    """How many periods/minutes per week a given course meets for a section.
    For rotating lab pairs, each subgroup gets that course only every other
    week, so this reports both the per-occurrence load and the per-subgroup
    weekly average."""
    course_code = course_code.upper()
    hits = sessions_for(section=section, course=course_code)
    period_len = KB["meta"]["period_length_minutes"]

    if not hits:
        return {"course": course_code, "section": section, "sessions_per_week": 0}

    total_periods = sum(len(s["periods"]) for s in hits)
    minutes = total_periods * period_len

    rotates = any(s.get("rotates") for s in hits)
    result = {
        "course": course_code,
        "title": KB["courses"][course_code]["title"],
        "section": section,
        "type": KB["courses"][course_code]["type"],
        "sessions_per_week_in_timetable": len(hits),
        "total_periods_per_week_in_timetable": total_periods,
        "total_minutes_per_week_in_timetable": minutes,
        "total_hours_per_week_in_timetable": round(minutes / 60, 2),
    }
    if rotates:
        # A rotating lab course only actually runs for a given subgroup every
        # other week, so the per-subgroup average is half.
        result["note"] = ("This is a lab that alternates weekly between subgroups "
                           "(see paired course). Each individual student attends "
                           "it every other week, i.e. ~%.2f hours/week on average, "
                           "or %d minutes every 2 weeks." % (minutes / 60 / 2, minutes))
    return result


# ---------------------------------------------------------------------------
# "Total number of labs" questions  (the tricky ones)
# ---------------------------------------------------------------------------

def total_labs_per_week(section: str) -> dict:
    """Counts distinct lab SESSIONS scheduled in a section's week.
    Two labs running in parallel in the same slot (one per subgroup) count
    as 2 sessions, because two separate classes are physically happening.
    """
    labs = [s for s in KB["sessions"] if s["section"] == section and s["type"] == "lab"]
    slots = defaultdict(list)
    for s in labs:
        slots[(s["day"], tuple(s["periods"]))].append(s["course"])

    return {
        "section": section,
        "total_lab_sessions_per_week": len(labs),
        "distinct_time_slots_used": len(slots),
        "breakdown": {f'{day} P{"-".join(map(str,periods))}': courses
                      for (day, periods), courses in slots.items()},
        "explanation": ("Some slots run two labs in parallel (one per subgroup, "
                         "e.g. CSE4102/CSE4112), which is why session count > slot count.")
    }


def total_labs_per_week_subgroup(subgroup: str) -> dict:
    """For a single subgroup (e.g. A1), count labs they personally attend in
    a given week -- rotating pairs only contribute ONE lab each (whichever
    course it is that week), not two."""
    section = subgroup[0]  # 'A' or 'B'
    labs = [s for s in KB["sessions"] if s["section"] == section and s["type"] == "lab"]

    fixed = [s for s in labs if not s.get("rotates") and s.get("subgroup") == subgroup]
    rotating_groups = defaultdict(list)
    for s in labs:
        if s.get("rotates"):
            rotating_groups[s["rotation_group"]].append(s)

    count = len(fixed)
    detail = [{"day": s["day"], "course": s["course"], "fixed": True} for s in fixed]
    for group_id, members in rotating_groups.items():
        # exactly one of `members` applies to this subgroup each week
        count += 1
        detail.append({
            "day": members[0]["day"],
            "course": f"{members[0]['course']} or {members[1]['course']} (alternates weekly)",
            "fixed": False,
        })

    return {
        "subgroup": subgroup,
        "total_labs_per_week": count,
        "detail": detail,
        "explanation": ("Fixed labs (e.g. CSE4106) happen every week for this subgroup. "
                         "Rotating pairs (e.g. CSE4102/CSE4112) count as ONE lab per week "
                         "for a given subgroup -- the specific course alternates week to week.")
    }


def free_periods(section: str, day: str):
    occupied = {p for s in sessions_for(section=section, day=day) for p in s["periods"]}
    all_periods = set(int(p) for p in KB["meta"]["periods"].keys())
    return sorted(all_periods - occupied)


if __name__ == "__main__":
    import pprint
    pp = pprint.PrettyPrinter(indent=2)
    print("=== Study hour of CSE4105 for Section A ===")
    pp.pprint(weekly_contact_minutes("CSE4105", "A"))
    print("\n=== Total labs/week for Section A ===")
    pp.pprint(total_labs_per_week("A"))
    print("\n=== Total labs/week for A1 ===")
    pp.pprint(total_labs_per_week_subgroup("A1"))
    print("\n=== Rotation this week for Section A ===")
    pp.pprint(lab_rotation_today("A"))
    print("\n=== Sunday schedule, Section A ===")
    pp.pprint(day_schedule("A", "Sunday"))
