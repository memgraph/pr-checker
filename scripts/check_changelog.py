#!/usr/bin/env python3
"""Validate that the PR has a filled-in release note in its template comment.

Skipped when the PR has the label named by SKIP_RELEASE_NOTE_LABEL. Otherwise,
searches PR issue comments for the template (anchored on the
`### Documentation checklist` heading) and requires at least one sub-bullet
under `Write a release note` that is non-empty and not the unfilled placeholder.
Only comments by the PR author, or by a repo owner, member or collaborator, count.

If `OPENAI_API_KEY` is set, the extracted note is then sent to an LLM for
a quality / format check. The model returns one of:
  - "pass":   resolve any earlier advisory comment, exit 0
  - "advise": upsert an advisory PR comment, exit 0
  - "block":  upsert an advisory PR comment, exit 1
If the API key is missing or the call fails, the LLM step is skipped.
"""

import json
import os
import re
import sys
import urllib.request

from _common import API_ERRORS, find_release_note, github_request, load_pull_request, paginate

DEFAULT_SKIP_LABEL = "docs-not-needed"
ADVISORY_MARKER = "<!-- pr-checker:changelog-advisory -->"
DEFAULT_MODEL = "gpt-5-mini"
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

SYSTEM_PROMPT = """\
You review release notes for the {repo} repository.

Required format (single line):
  "<short statement of changes> [#<PR number>](<matching PR URL>)"

Example:
  "Added a new configuration option to enable remote monitoring. \
[#1234](https://github.com/{repo}/pull/1234)"

Rules:
- One short sentence describing the change, ending in a period.
- Immediately followed by a space and the markdown link "[#N](URL)"
  where N is the PR number and URL is the PR's html_url.
- The PR number and URL MUST match the PR provided.
- Humour is fine as long as it's professional and friendly.
- No profanity, slurs, or otherwise rude language.
- Must be meaningful content, not gibberish or leftover placeholder text.

Classify the note as exactly one of:
- "pass":   meets the format and is sensible.
- "advise": minor issues only (typos, grammar, slightly off format, trailing
            whitespace, etc.) -- worth flagging but not worth blocking the PR.
- "block":  severe issues -- profanity, gibberish, missing/wrong PR link,
            empty, or wildly off-format.

Keep "reason" to one or two sentences. If "pass", reason can be empty.
"""


def skip_label() -> str:
    return os.environ.get("SKIP_RELEASE_NOTE_LABEL", DEFAULT_SKIP_LABEL).strip()


def has_skip_label(pr: dict, label: str) -> bool:
    return bool(label) and label in (lbl["name"] for lbl in pr.get("labels", []))


def fetch_comments(pr: dict, token: str) -> list[dict]:
    comments_url = pr.get("comments_url")
    if not comments_url:
        return []
    return [
        {
            "id": c["id"],
            "body": c.get("body") or "",
            "login": (c.get("user") or {}).get("login"),
            "association": c.get("author_association"),
        }
        for page in paginate(f"{comments_url}?per_page=100", token)
        for c in page
    ]


def is_trusted(comment: dict, pr: dict) -> bool:
    author = (pr.get("user") or {}).get("login")
    return (author is not None and comment["login"] == author) or comment["association"] in TRUSTED_ASSOCIATIONS


def find_note(comments: list[dict], pr: dict) -> str | None:
    for comment in comments:
        note = find_release_note(comment["body"])
        if not note:
            continue
        if is_trusted(comment, pr):
            return note
        print(
            f"::notice::Ignoring the release note in a comment by @{comment['login']}: "
            "only the PR author or a repo owner, member or collaborator can supply it."
        )
    return None


def evaluate_note(note: str, pr: dict) -> dict | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("::warning::OPENAI_API_KEY not set; skipping LLM quality check.")
        return None

    repo = pr.get("base", {}).get("repo", {}).get("full_name") or os.environ.get("GITHUB_REPOSITORY", "")
    payload = {
        "model": os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(repo=repo)},
            {
                "role": "user",
                "content": (
                    f"PR number: #{pr.get('number')}\n"
                    f"PR URL: {pr.get('html_url')}\n\n"
                    f"Release note to evaluate:\n{note}"
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "release_note_verdict",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "verdict": {"type": "string", "enum": ["pass", "advise", "block"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["verdict", "reason"],
                },
            },
        },
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        # A refusal has null content, and the model is not guaranteed to return an object.
        verdict = json.loads(content) if isinstance(content, str) else None
    except (*API_ERRORS, LookupError, TypeError) as exc:
        print(f"::warning::OpenAI quality check failed ({exc!r}); skipping.")
        return None

    if not isinstance(verdict, dict) or verdict.get("verdict") not in {"pass", "advise", "block"}:
        print(f"::warning::Unexpected response from model: {verdict!r}; skipping.")
        return None
    return verdict


def find_advisory(comments: list[dict]) -> dict | None:
    return next((c for c in comments if c["body"].startswith(ADVISORY_MARKER)), None)


def write_advisory(pr: dict, token: str, advisory: dict | None, body_text: str) -> None:
    """Create the advisory comment, or update it in place if it exists."""
    full_body = f"{ADVISORY_MARKER}\n{body_text}"
    if advisory and advisory["body"] == full_body:
        return
    try:
        if advisory:
            url = f"{pr['base']['repo']['url']}/issues/comments/{advisory['id']}"
            with github_request(url, token, method="PATCH", body={"body": full_body}):
                pass
        else:
            with github_request(pr["comments_url"], token, method="POST", body={"body": full_body}):
                pass
    except API_ERRORS as exc:
        print(f"::warning::Could not write the advisory comment ({exc!r}); continuing.")


def resolve_advisory(pr: dict, token: str, advisory: dict | None, body_text: str) -> None:
    """Mark an earlier advisory as resolved, so a stale "blocked" comment doesn't linger."""
    if advisory:
        write_advisory(pr, token, advisory, f"**Release note advisory resolved**\n\n{body_text}")


def code_span(text: str) -> str:
    # Inside a code span, @mentions don't notify and markdown doesn't render.
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def defuse_mentions(text: str) -> str:
    # A zero-width space after "@" stops GitHub treating it as a mention.
    return re.sub(r"@(?=[A-Za-z0-9])", "@​", text)


def format_advisory(verdict: dict, note: str) -> str:
    level = verdict["verdict"]
    header = {
        "advise": "**Release note advisory**",
        "block":  "**Release note blocked**",
    }[level]
    reason = defuse_mentions(str(verdict.get("reason") or "")) or "(no reason provided)"
    return f"{header}\n\nRelease note: {code_span(note)}\n\n{reason}"


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("::error::GITHUB_TOKEN is required to fetch the PR and its comments.")
        return 1

    pr = load_pull_request(token)
    if not pr:
        print("::error::Not running in a pull_request event; cannot load PR.")
        return 1

    try:
        comments = fetch_comments(pr, token)
    except API_ERRORS as exc:
        print(f"::error::Could not fetch PR comments ({exc!r}).")
        return 1
    advisory = find_advisory(comments)

    label = skip_label()
    if has_skip_label(pr, label):
        print(f"Skipping release-note check: '{label}' label is set.")
        resolve_advisory(pr, token, advisory, f"No release note is needed: the `{label}` label is set.")
        return 0

    note = find_note(comments, pr)
    if not note:
        hint = f" (or add the '{label}' label if no note is required)" if label else ""
        print(
            "::error::No release note found. Copy the PR template into a comment and fill in the "
            f"'Write a release note' sub-bullet{hint}."
        )
        return 1

    print(f"Found release note: {note}")

    verdict = evaluate_note(note, pr)
    if verdict is None:
        return 0
    if verdict["verdict"] == "pass":
        resolve_advisory(pr, token, advisory, f"Release note: {code_span(note)}")
        return 0

    write_advisory(pr, token, advisory, format_advisory(verdict, note))

    if verdict["verdict"] == "block":
        print(f"::error::Release note blocked by quality check: {verdict.get('reason')}")
        return 1

    print(f"::warning::Release note advisory: {verdict.get('reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
