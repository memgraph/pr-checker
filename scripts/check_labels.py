#!/usr/bin/env python3
"""Validate that a pull request has the required labels and a milestone.

Configured through environment variables (set by action.yml):
  DOCS_LABELS:       comma-separated; exactly one must be present (empty disables)
  COMPONENT_LABELS:  comma-separated; at least one must be present (empty disables)
  TYPE_LABELS:       comma-separated; at least one must be present (empty disables)
  REQUIRE_MILESTONE: "true" / "false"

Outside a pull_request event, PR_LABELS and PR_MILESTONE can be set for local testing.
"""

import os
import sys

from _common import load_pull_request, parse_bool, parse_list


def load_labels(pr: dict) -> list[str]:
    if pr:
        return [label["name"] for label in pr.get("labels", [])]
    return parse_list(os.environ.get("PR_LABELS"))


def milestone_title(pr: dict) -> str | None:
    # When run outside a PR event, fall back to PR_MILESTONE for local testing.
    if pr:
        return (pr.get("milestone") or {}).get("title")
    return os.environ.get("PR_MILESTONE") or None


def check_milestone(pr: dict) -> str | None:
    if not milestone_title(pr):
        return "Missing milestone. Assign the PR to a milestone before merging."
    return None


def check_exclusive(labels: list[str], group: list[str], description: str) -> str | None:
    if not group:
        return None
    matches = [label for label in group if label in labels]
    if not matches:
        return f"Missing a {description} label. Add exactly one of: {', '.join(group)}."
    if len(matches) > 1:
        return (
            f"Found multiple {description} labels ({', '.join(matches)}). "
            f"Only one of {', '.join(group)} is allowed."
        )
    return None


def check_inclusive(labels: list[str], group: list[str], description: str) -> str | None:
    if not group:
        return None
    matches = [label for label in group if label in labels]
    if not matches:
        return f"Missing a {description} label. Add at least one of: {', '.join(group)}."
    return None


def main() -> int:
    pr = load_pull_request()
    labels = load_labels(pr)
    require_milestone = parse_bool(os.environ.get("REQUIRE_MILESTONE"), default=True)

    errors = [
        err
        for err in (
            check_exclusive(labels, parse_list(os.environ.get("DOCS_LABELS")), "docs"),
            check_inclusive(labels, parse_list(os.environ.get("COMPONENT_LABELS")), "component"),
            check_inclusive(labels, parse_list(os.environ.get("TYPE_LABELS")), "type"),
            check_milestone(pr) if require_milestone else None,
        )
        if err
    ]

    if errors:
        for err in errors:
            print(f"::error::{err}")
        return 1

    print(f"Labels on PR: {', '.join(labels) if labels else '(none)'}")
    print(f"Milestone: {milestone_title(pr)}")
    print("All label checks satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
