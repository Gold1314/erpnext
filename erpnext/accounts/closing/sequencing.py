# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Pure dependency/date sequencing utilities for the Financial Close Manager.

This module is deliberately frappe-free so the close checklist's ordering
rules can be unit tested without a site (see ``test_sequencing.py``). The
Close Cycle / Close Task controllers are thin adapters over these functions.

Vocabulary
----------
- A *row* is a template task: ``{"title": str, "depends_on_titles": str}``
  where ``depends_on_titles`` is a comma-separated list of other titles in
  the same template (or an already-split list).
- A *dependency map* is ``{title: [dep_title, ...]}`` in row order.
- A *status map* is ``{task_key: status}`` where a dependency is satisfied
  once its status is Completed or Skipped.
"""

from __future__ import annotations

import datetime

#: Task statuses that satisfy a dependency.
DONE_STATUSES = ("Completed", "Skipped")


def split_titles(value) -> list[str]:
	"""Normalize a comma-separated ``depends_on_titles`` value to a clean list."""
	if not value:
		return []
	parts = value.split(",") if isinstance(value, str) else list(value)
	return [str(part).strip() for part in parts if str(part).strip()]


def resolve_dependencies(rows: list[dict]) -> dict[str, list[str]]:
	"""Resolve per-title dependency lists for a set of template rows.

	Validates that every title is present and unique, that no task depends on
	itself or on an unknown title, and that the graph is acyclic. Raises
	``ValueError`` with a human-readable message on any violation; for cycles
	the message contains the offending path (``A -> B -> A``).
	"""
	titles: list[str] = []
	for row in rows:
		title = (row.get("title") or "").strip()
		if not title:
			raise ValueError("Every task needs a title")
		if title in titles:
			raise ValueError(f"Duplicate task title '{title}' - titles must be unique within a template")
		titles.append(title)

	known = set(titles)
	dependency_map: dict[str, list[str]] = {}
	for row in rows:
		title = row["title"].strip()
		deps = split_titles(row.get("depends_on_titles"))
		for dep in deps:
			if dep == title:
				raise ValueError(f"Task '{title}' cannot depend on itself")
			if dep not in known:
				raise ValueError(f"Task '{title}' depends on unknown task '{dep}'")
		# de-duplicate while preserving order
		seen: set[str] = set()
		dependency_map[title] = [d for d in deps if not (d in seen or seen.add(d))]

	_raise_on_cycle(dependency_map)
	return dependency_map


def _raise_on_cycle(dependency_map: dict[str, list[str]]) -> None:
	"""DFS cycle detection; raises ValueError carrying the cycle path."""
	UNSEEN, IN_STACK, DONE = 0, 1, 2
	state = dict.fromkeys(dependency_map, UNSEEN)

	def visit(node: str, path: list[str]) -> None:
		state[node] = IN_STACK
		path.append(node)
		for dep in dependency_map.get(node, ()):
			if state[dep] == IN_STACK:
				cycle = [*path[path.index(dep) :], dep]
				raise ValueError("Dependency cycle detected: " + " -> ".join(cycle))
			if state[dep] == UNSEEN:
				visit(dep, path)
		path.pop()
		state[node] = DONE

	for title in dependency_map:
		if state[title] == UNSEEN:
			visit(title, [])


def topological_order(dependency_map: dict[str, list[str]]) -> list[str]:
	"""Return titles so every task appears after all of its dependencies.

	Deterministic: ties are broken by the original row order of
	``dependency_map`` (dicts preserve insertion order). Assumes the map has
	already been validated by :func:`resolve_dependencies`.
	"""
	placed: list[str] = []
	placed_set: set[str] = set()
	remaining = list(dependency_map)

	while remaining:
		progressed = False
		for title in list(remaining):
			if all(dep in placed_set for dep in dependency_map[title]):
				placed.append(title)
				placed_set.add(title)
				remaining.remove(title)
				progressed = True
		if not progressed:  # pragma: no cover - resolve_dependencies rejects cycles first
			raise ValueError("Dependency cycle detected among: " + ", ".join(sorted(remaining)))

	return placed


def compute_due_date(period_end: datetime.date, offset_days: int) -> datetime.date:
	"""Due date for a task: period end plus an offset in *calendar* days.

	The template labels the offset "days after period end"; we intentionally
	use calendar days (not business days) to stay deterministic without a
	holiday calendar. A task due "T+2" after a Friday period end lands on
	Sunday - teams that close on business days should size offsets to suit.
	"""
	return period_end + datetime.timedelta(days=int(offset_days or 0))


def blocking_dependencies(status_map: dict[str, str], dep_keys: list[str]) -> list[str]:
	"""Return the dependencies (in order) that are not yet Completed/Skipped.

	A dependency missing from ``status_map`` is treated as blocking.
	"""
	return [key for key in dep_keys if status_map.get(key) not in DONE_STATUSES]


def is_unblocked(status_map: dict[str, str], dep_keys: list[str]) -> bool:
	"""True when every dependency is Completed or Skipped."""
	return not blocking_dependencies(status_map, dep_keys)
