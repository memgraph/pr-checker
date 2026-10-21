"""Shared helpers for the PR checker scripts."""

import http.client
import json
import os
import re
import urllib.request

TEMPLATE_ANCHOR = "### Documentation checklist"
RELEASE_NOTE_HEADING = re.compile(r"^\s*-\s*\[.\]\s*Write a release note\b", re.IGNORECASE)
PLACEHOLDER_QUESTION = "What has changed? What does it mean for a user? What should a user do with it?"
PLACEHOLDER_TEXT = f"{PLACEHOLDER_QUESTION} [#{{{{PR_number}}}}]({{{{link to the PR}}}})"
LIST_MARKER = re.compile(r"^[-*+]\s*(\[.\]\s*)?")
LIST_ITEM = re.compile(r"^[-*+](\s|$)")

# Network and response-parsing errors from the GitHub and OpenAI APIs. URLError,
# HTTPError, timeouts and connection resets are OSErrors; dropped connections
# and truncated reads are HTTPExceptions; bad JSON is a ValueError.
API_ERRORS = (OSError, http.client.HTTPException, ValueError)


def api_url(path: str) -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/") + path


def load_event() -> dict:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.isfile(event_path):
        with open(event_path, encoding="utf-8") as f:
            return json.load(f) or {}
    return {}


def load_pull_request(token: str | None) -> dict:
    """Return the PR this event is about, fetched live from the API.

    The event payload is a snapshot from when the event fired, so a re-run would
    see stale labels and milestone. Falls back to the payload if the fetch fails.
    """
    event = load_event()
    payload_pr = event.get("pull_request") or {}
    number = payload_pr.get("number")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (number and repo and token):
        return payload_pr
    try:
        with github_request(api_url(f"/repos/{repo}/pulls/{number}"), token) as resp:
            return json.loads(resp.read())
    except API_ERRORS as exc:
        print(f"::warning::Could not fetch PR #{number} ({exc}); using the event payload.")
        return payload_pr


def parse_list(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def github_request(url: str, token: str, *, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urllib.request.urlopen(req, timeout=30)


def next_page_url(link_header: str) -> str | None:
    for part in link_header.split(","):
        m = re.search(r"<([^>]+)>\s*;\s*rel=\"next\"", part)
        if m:
            return m.group(1)
    return None


def paginate(url: str, token: str):
    while url:
        with github_request(url, token) as resp:
            yield json.loads(resp.read())
            link = resp.headers.get("Link", "")
        url = next_page_url(link)


def is_placeholder(text: str) -> bool:
    # Authors often delete only the link and keep the question, so match on the
    # question alone.
    return " ".join(text.split()).casefold().startswith(PLACEHOLDER_QUESTION.casefold())


def find_release_note(body: str) -> str | None:
    """Return the first filled-in sub-bullet under "Write a release note".

    A sub-bullet's indented continuation lines are joined to it, so a note that
    wraps keeps its trailing PR link. The section ends at the next unindented line.
    """
    if TEMPLATE_ANCHOR not in body:
        return None

    lines = body.splitlines()
    try:
        heading_idx = next(i for i, line in enumerate(lines) if RELEASE_NOTE_HEADING.match(line))
    except StopIteration:
        return None

    items: list[list[str]] = []
    current: list[str] | None = None
    for line in lines[heading_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            current = None
            continue
        if not line.startswith((" ", "\t")):
            break
        if LIST_ITEM.match(stripped) or current is None:
            current = [LIST_MARKER.sub("", stripped).strip()]
            items.append(current)
        else:
            current.append(stripped)

    for parts in items:
        content = " ".join(part for part in parts if part)
        if content and not is_placeholder(content):
            return content
    return None
