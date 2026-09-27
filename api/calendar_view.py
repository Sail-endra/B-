"""One unified calendar feed and its progress arithmetic.

Merges four sources into a single, normalised list of dated items:

  * `syllabus_schedule` -- dated lecture rows (the concepts taught that day) and
    any dated due/exam rows the extractor found;
  * `assessments`       -- graded work with a due date and a completion status;
  * `calendar_events`   -- user-created events, including recurring class meetings
    with a time and location (which the syllabus text rarely yields cleanly);
  * US holidays         -- computed, shown shaded and non-interactive.

Every item carries a stable `id` so it can be checked off. "Work" for the
progress rings is the set of tasks a student completes -- assessments, deadlines,
exams, projects, and the day's concepts -- never class meetings or holidays.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Optional

_WEEKDAY_CODE = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
# Kinds that count as "work" the student checks off (vs. class meetings/holidays).
# Kinds the student completes (checked off, counted as work). "class", "club" and
# "job" are recurring commitments they attend, not tasks, so they are excluded.
_TASK_KINDS = {"deadline", "due", "exam", "project", "research", "meeting",
               "custom", "homework", "problem_set", "paper", "quiz",
               "reading_response", "presentation", "midterm", "final", "concept"}


# -- holidays ---------------------------------------------------------------


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth (1-based) `weekday` (Mon=0) of a month; n=-1 means the last."""
    if n > 0:
        d = date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + timedelta(days=offset + 7 * (n - 1))
    d = date(year, month, 28) + timedelta(days=4)
    d = d.replace(day=1) - timedelta(days=1)  # last day of month
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def us_holidays(year: int) -> dict[date, str]:
    """Federal holidays plus the common academic Thanksgiving Friday. School
    breaks vary by institution, so students add those as events."""
    h = {
        date(year, 1, 1): "New Year's Day",
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Presidents' Day",
        _nth_weekday(year, 5, 0, -1): "Memorial Day",
        date(year, 6, 19): "Juneteenth",
        date(year, 7, 4): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 10, 0, 2): "Indigenous Peoples' Day",
        date(year, 11, 11): "Veterans Day",
        date(year, 12, 25): "Christmas Day",
    }
    thanksgiving = _nth_weekday(year, 11, 3, 4)  # 4th Thursday
    h[thanksgiving] = "Thanksgiving"
    h[thanksgiving + timedelta(days=1)] = "Thanksgiving break"
    return h


# -- merging ----------------------------------------------------------------


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _course_codes(store, user: str) -> dict[str, str]:
    return {c.course_id: c.code for c in store.courses(user)}


def _expand_event(ev: dict, start: date, end: date) -> list[date]:
    """Concrete dates this event falls on within [start, end]."""
    if ev.get("recurrence") == "weekly" and ev.get("recur_days"):
        days = {c for c in (ev["recur_days"] or "").split(",") if c}
        begin = _parse(ev.get("date")) or start
        until = _parse(ev.get("recur_until")) or end
        out = []
        for d in _daterange(max(start, begin), min(end, until)):
            if _WEEKDAY_CODE[d.weekday()] in days:
                out.append(d)
        return out
    d = _parse(ev.get("date"))
    return [d] if d and start <= d <= end else []


def _parse(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def calendar_items(store, user: str, start: date, end: date) -> list[dict[str, Any]]:
    codes = _course_codes(store, user)
    done = store.task_done(user)
    items: list[dict[str, Any]] = []

    def code(cid: str) -> str:
        return codes.get(cid, cid or "")

    # 1. Syllabus schedule (dated) -- lectures carry the day's concepts.
    for r in store.schedule(user):
        d = _parse(r.get("date"))
        if not d or not (start <= d <= end):
            continue
        raw_kind = (r.get("kind") or "lecture").lower()
        kind = "concept" if raw_kind in ("lecture", "class") else raw_kind
        iid = f"sched:{r['id']}"
        items.append({
            "id": iid, "date": d.isoformat(), "kind": kind,
            "title": r.get("topic") or r.get("link_title") or "Class",
            "course_id": r.get("course_id", ""), "course_code": code(r.get("course_id", "")),
            "start_time": "", "end_time": "", "location": "",
            "chapter_refs": _as_list(r.get("chapter_refs")),
            "readings": _as_list(r.get("readings")),
            "done": iid in done, "editable": False, "source": "syllabus",
        })

    # 2. Assessments -- graded work with its own completion status.
    for a in store.assessments(user):
        d = a.due_date
        if not d or not (start <= d <= end):
            continue
        items.append({
            "id": f"assess:{a.id}", "date": d.isoformat(),
            "kind": a.kind.value, "title": a.title or a.kind.label,
            "course_id": a.course_id, "course_code": code(a.course_id),
            "start_time": "", "end_time": "", "location": "",
            "chapter_refs": list(a.chapter_refs or []), "readings": [],
            "done": a.status.value == "done", "editable": True, "source": "assessment",
            "weight": a.weight, "status": a.status.value,
        })

    # 3. User events (one-off + recurring classes with time/location).
    for ev in store.events(user):
        for d in _expand_event(ev, start, end):
            recurring = ev.get("recurrence") == "weekly"
            iid = f"event:{ev['id']}" + (f":{d.isoformat()}" if recurring else "")
            items.append({
                "id": iid, "date": d.isoformat(), "kind": ev.get("kind", "custom"),
                "title": ev.get("title", "Event"),
                "course_id": ev.get("course_id", ""), "course_code": code(ev.get("course_id", "")),
                "start_time": ev.get("start_time", ""), "end_time": ev.get("end_time", ""),
                "location": ev.get("location", ""), "chapter_refs": [], "readings": [],
                "notes": ev.get("notes", ""),
                "done": (iid in done) or (not recurring and bool(ev.get("done"))),
                "editable": True, "source": "event", "recurring": recurring,
                "event_id": ev["id"],
            })

    # 4. Holidays.
    for yr in range(start.year, end.year + 1):
        for hd, name in us_holidays(yr).items():
            if start <= hd <= end:
                items.append({
                    "id": f"holiday:{hd.isoformat()}", "date": hd.isoformat(),
                    "kind": "holiday", "title": name, "course_id": "", "course_code": "",
                    "start_time": "", "end_time": "", "location": "", "chapter_refs": [],
                    "readings": [], "done": False, "editable": False, "source": "holiday",
                })

    items.sort(key=lambda i: (i["date"], i.get("start_time") or "zz", i["title"]))
    return items


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return []
    return []


# -- progress ---------------------------------------------------------------


def _count(items: list[dict]) -> dict[str, Any]:
    work = [i for i in items if i["kind"] in _TASK_KINDS]
    done = sum(1 for i in work if i["done"])
    total = len(work)
    return {"total": total, "done": done,
            "pct": round(100 * done / total) if total else 0}


def semester_bounds(store, user: str, today: date) -> tuple[date, date]:
    """The student's actual term span: the earliest to latest dated item, widened
    to include today. Falls back to a term-length window around today."""
    dates: list[date] = []
    for r in store.schedule(user):
        d = _parse(r.get("date"))
        if d:
            dates.append(d)
    for a in store.assessments(user):
        if a.due_date:
            dates.append(a.due_date)
    for ev in store.events(user):
        d = _parse(ev.get("date"))
        if d:
            dates.append(d)
        u = _parse(ev.get("recur_until"))
        if u:
            dates.append(u)
    if dates:
        return min(min(dates), today), max(max(dates), today)
    return today - timedelta(days=120), today + timedelta(days=120)


def progress(store, user: str, today: date) -> dict[str, Any]:
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    sem_start, sem_end = semester_bounds(store, user, today)
    return {
        "today": _count(calendar_items(store, user, today, today)),
        "week": _count(calendar_items(store, user, monday, sunday)),
        "semester": _count(calendar_items(store, user, sem_start, sem_end)),
        "week_range": [monday.isoformat(), sunday.isoformat()],
        "semester_range": [sem_start.isoformat(), sem_end.isoformat()],
    }
