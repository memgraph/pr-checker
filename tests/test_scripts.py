"""Unit tests for the PR checker scripts. Run with: python -m unittest discover -s tests"""

import http.client
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_changelog  # noqa: E402
import check_labels  # noqa: E402
from _common import (  # noqa: E402
    PLACEHOLDER_QUESTION,
    PLACEHOLDER_TEXT,
    find_release_note,
    load_pull_request,
    next_page_url,
    parse_bool,
    parse_list,
)

API = "https://api.github.com"
REPO = "o/r"
NOTE = "Added a thing. [#1](https://github.com/o/r/pull/1)"

TEMPLATE = """\
### Documentation checklist
- [x] Write a release note, including added/changed clauses
    - {note}
- [ ] **[ Documentation PR link memgraph/documentation#XXXX ]**
"""


def checklist(*sub_lines: str) -> str:
    body = "### Documentation checklist\n- [x] Write a release note, including added/changed clauses\n"
    return body + "".join(f"{line}\n" for line in sub_lines) + "- [ ] **[ Documentation PR link ]**\n"


def make_pr(labels=(), milestone="v1", author="alice", number=1):
    return {
        "number": number,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "comments_url": f"{API}/repos/{REPO}/issues/{number}/comments",
        "labels": [{"name": n} for n in labels],
        "milestone": {"title": milestone} if milestone else None,
        "user": {"login": author},
        "head": {"sha": "abc123"},
        "base": {"repo": {"url": f"{API}/repos/{REPO}", "full_name": REPO}},
    }


def make_comment(body, login="alice", association="CONTRIBUTOR", comment_id=10):
    return {"id": comment_id, "body": body, "user": {"login": login}, "author_association": association}


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}


class FakeAPI:
    """Stands in for urllib.request.urlopen, answering GitHub and OpenAI requests by route."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None):
        method, url = req.get_method(), req.full_url
        body = json.loads(req.data) if req.data else None
        self.calls.append((method, url, body))
        for (route_method, prefix), answer in self.routes.items():
            if method == route_method and url.startswith(prefix):
                if isinstance(answer, BaseException):
                    raise answer
                return FakeResponse(answer(body) if callable(answer) else answer)
        raise AssertionError(f"Unexpected request: {method} {url}")

    def writes(self):
        return [(m, u, b) for m, u, b in self.calls if m != "GET" and u.startswith(API)]


def openai_reply(verdict, reason=""):
    return {"choices": [{"message": {"content": json.dumps({"verdict": verdict, "reason": reason})}}]}


class EventTestCase(unittest.TestCase):
    def write_event(self, event):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(event, f)
        self.addCleanup(os.unlink, f.name)
        return f.name

    def run_script(self, module, event, api, **env):
        base_env = {
            "GITHUB_EVENT_PATH": self.write_event(event),
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_TOKEN": "t",
        }
        with mock.patch.dict(os.environ, {**base_env, **env}, clear=True), \
                mock.patch("urllib.request.urlopen", api), \
                mock.patch("builtins.print") as printed:
            code = module.main()
        self.output = "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        return code


class FindReleaseNoteTest(unittest.TestCase):
    def test_filled_note(self):
        self.assertEqual(find_release_note(TEMPLATE.format(note=NOTE)), NOTE)

    def test_placeholder_is_ignored(self):
        self.assertIsNone(find_release_note(TEMPLATE.format(note=PLACEHOLDER_TEXT)))

    def test_placeholder_without_link_is_ignored(self):
        self.assertIsNone(find_release_note(checklist(f"    - {PLACEHOLDER_QUESTION}")))

    def test_note_after_placeholder_without_link(self):
        self.assertEqual(find_release_note(checklist(f"    - {PLACEHOLDER_QUESTION}", f"    - {NOTE}")), NOTE)

    def test_wrapped_note_keeps_its_link(self):
        body = checklist("    - Added a thing that needed a long", "      explanation. [#1](https://github.com/o/r/pull/1)")
        self.assertEqual(
            find_release_note(body),
            "Added a thing that needed a long explanation. [#1](https://github.com/o/r/pull/1)",
        )

    def test_next_sibling_bullet_ends_the_note(self):
        self.assertEqual(find_release_note(checklist(f"    - {NOTE}", "    - Second note.")), NOTE)

    def test_missing_anchor(self):
        self.assertIsNone(find_release_note("- [x] Write a release note\n    - Something."))

    def test_unindented_line_ends_section(self):
        body = "### Documentation checklist\n- [x] Write a release note\n- [ ] Other item\n    - Not a note.\n"
        self.assertIsNone(find_release_note(body))

    def test_crlf_line_endings(self):
        self.assertEqual(find_release_note(TEMPLATE.format(note=NOTE).replace("\n", "\r\n")), NOTE)


class ParseTest(unittest.TestCase):
    def test_parse_list(self):
        self.assertEqual(parse_list(" a, b ,,c "), ["a", "b", "c"])
        self.assertEqual(parse_list(""), [])
        self.assertEqual(parse_list(None), [])

    def test_parse_bool(self):
        self.assertTrue(parse_bool("true", default=False))
        self.assertFalse(parse_bool("false", default=True))
        self.assertTrue(parse_bool("", default=True))
        self.assertTrue(parse_bool(None, default=True))

    def test_next_page_url(self):
        header = '<https://x/?page=2>; rel="next", <https://x/?page=5>; rel="last"'
        self.assertEqual(next_page_url(header), "https://x/?page=2")
        self.assertIsNone(next_page_url('<https://x/?page=5>; rel="last"'))


class LoadPullRequestTest(EventTestCase):
    def load(self, event, api, **env):
        base_env = {"GITHUB_EVENT_PATH": self.write_event(event), "GITHUB_REPOSITORY": REPO}
        with mock.patch.dict(os.environ, {**base_env, **env}, clear=True), \
                mock.patch("urllib.request.urlopen", api), \
                mock.patch("builtins.print"):
            return load_pull_request("t")

    def test_fetches_live_pr_for_pull_request_event(self):
        live = make_pr(labels=["bug"])
        pr = self.load({"pull_request": make_pr(labels=[])}, FakeAPI({("GET", f"{API}/repos/{REPO}/pulls/1"): live}))
        self.assertEqual(pr["labels"], [{"name": "bug"}])

    def test_falls_back_to_payload_when_fetch_fails(self):
        api = FakeAPI({("GET", f"{API}/repos/{REPO}/pulls/1"): http.client.RemoteDisconnected("gone")})
        pr = self.load({"pull_request": make_pr(labels=["stale"])}, api)
        self.assertEqual(pr["labels"], [{"name": "stale"}])

    def test_uses_github_api_url(self):
        api = FakeAPI({("GET", "https://ghe.example/api/v3/repos/o/r/pulls/1"): make_pr()})
        self.load({"pull_request": make_pr()}, api, GITHUB_API_URL="https://ghe.example/api/v3")
        self.assertEqual(len(api.calls), 1)


class CheckLabelsTest(EventTestCase):
    def run_main(self, labels, milestone, **env):
        pr = make_pr(labels=labels, milestone=milestone)
        api = FakeAPI({("GET", f"{API}/repos/{REPO}/pulls/1"): pr})
        base_env = {
            "DOCS_LABELS": "docs-needed,docs-not-needed",
            "COMPONENT_LABELS": "",
            "TYPE_LABELS": "bug,feature",
            "REQUIRE_MILESTONE": "true",
        }
        return self.run_script(check_labels, {"pull_request": pr}, api, **{**base_env, **env})

    def test_passes(self):
        self.assertEqual(self.run_main(["docs-needed", "bug"], "v1"), 0)

    def test_missing_milestone(self):
        self.assertEqual(self.run_main(["docs-needed", "bug"], None), 1)

    def test_milestone_not_required(self):
        self.assertEqual(self.run_main(["docs-needed", "bug"], None, REQUIRE_MILESTONE="false"), 0)

    def test_multiple_docs_labels(self):
        self.assertEqual(self.run_main(["docs-needed", "docs-not-needed", "bug"], "v1"), 1)

    def test_component_required_when_configured(self):
        self.assertEqual(self.run_main(["docs-needed", "bug"], "v1", COMPONENT_LABELS="api,ui"), 1)
        self.assertEqual(self.run_main(["docs-needed", "bug", "ui"], "v1", COMPONENT_LABELS="api,ui"), 0)

    def test_empty_group_disables_check(self):
        self.assertEqual(self.run_main(["bug"], "v1", DOCS_LABELS=""), 0)

    def test_uses_live_labels_not_the_payload(self):
        # A re-run reuses the original payload, which may predate the label being added.
        live = make_pr(labels=["docs-needed", "bug"])
        api = FakeAPI({("GET", f"{API}/repos/{REPO}/pulls/1"): live})
        event = {"pull_request": make_pr(labels=[])}
        env = {"DOCS_LABELS": "docs-needed", "TYPE_LABELS": "bug", "REQUIRE_MILESTONE": "true"}
        self.assertEqual(self.run_script(check_labels, event, api, **env), 0)


class CheckChangelogTest(EventTestCase):
    def run_main(self, comments, labels=(), openai=None, **env):
        pr = make_pr(labels=labels)
        routes = {
            ("GET", f"{API}/repos/{REPO}/pulls/1"): pr,
            ("GET", pr["comments_url"]): comments,
            ("POST", pr["comments_url"]): {},
            ("PATCH", f"{API}/repos/{REPO}/issues/comments/"): {},
        }
        if openai is not None:
            routes[("POST", "https://api.openai.com/")] = openai
            env.setdefault("OPENAI_API_KEY", "k")
        self.api = FakeAPI(routes)
        return self.run_script(check_changelog, {"pull_request": pr}, self.api, **env)

    def advisory(self, text="**Release note blocked**", comment_id=99):
        return make_comment(f"{check_changelog.ADVISORY_MARKER}\n{text}", login="github-actions[bot]",
                            association="NONE", comment_id=comment_id)

    def test_passes_without_openai_key(self):
        self.assertEqual(self.run_main([make_comment(TEMPLATE.format(note=NOTE))]), 0)
        self.assertEqual(self.api.writes(), [])

    def test_fails_without_note(self):
        self.assertEqual(self.run_main([make_comment(TEMPLATE.format(note=PLACEHOLDER_TEXT))]), 1)

    def test_skip_label(self):
        self.assertEqual(self.run_main([], labels=["docs-not-needed"]), 0)

    def test_has_skip_label(self):
        pr = {"labels": [{"name": "docs-not-needed"}]}
        self.assertTrue(check_changelog.has_skip_label(pr, "docs-not-needed"))
        self.assertFalse(check_changelog.has_skip_label(pr, "other"))
        self.assertFalse(check_changelog.has_skip_label(pr, ""))

    def test_ignores_note_from_untrusted_commenter(self):
        outsider = make_comment(TEMPLATE.format(note="Spam. [#1](https://x)"), login="mallory", comment_id=1)
        author = make_comment(TEMPLATE.format(note=NOTE), login="alice", comment_id=2)
        self.assertEqual(self.run_main([outsider, author]), 0)
        self.assertIn(f"Found release note: {NOTE}", self.output)
        self.assertIn("@mallory", self.output)

    def test_accepts_note_from_member(self):
        member = make_comment(TEMPLATE.format(note=NOTE), login="bob", association="MEMBER")
        self.assertEqual(self.run_main([member]), 0)

    def test_fails_when_only_untrusted_note(self):
        self.assertEqual(self.run_main([make_comment(TEMPLATE.format(note=NOTE), login="mallory")]), 1)

    def test_block_posts_escaped_advisory(self):
        note = "Thanks @everyone. [#1](https://github.com/o/r/pull/1)"
        code = self.run_main([make_comment(TEMPLATE.format(note=note))], openai=openai_reply("block", "Ping @team."))
        self.assertEqual(code, 1)
        [(method, _, body)] = self.api.writes()
        self.assertEqual(method, "POST")
        self.assertIn(f"`{note}`", body["body"])
        self.assertIn("@​team", body["body"])

    def test_advise_updates_existing_advisory(self):
        comments = [make_comment(TEMPLATE.format(note=NOTE)), self.advisory()]
        self.assertEqual(self.run_main(comments, openai=openai_reply("advise", "Typo.")), 0)
        [(method, url, _)] = self.api.writes()
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.endswith("/issues/comments/99"))

    def test_pass_resolves_existing_advisory(self):
        comments = [make_comment(TEMPLATE.format(note=NOTE)), self.advisory()]
        self.assertEqual(self.run_main(comments, openai=openai_reply("pass")), 0)
        [(method, _, body)] = self.api.writes()
        self.assertEqual(method, "PATCH")
        self.assertIn("resolved", body["body"])

    def test_pass_without_advisory_writes_nothing(self):
        self.assertEqual(self.run_main([make_comment(TEMPLATE.format(note=NOTE))], openai=openai_reply("pass")), 0)
        self.assertEqual(self.api.writes(), [])

    def test_skip_label_resolves_existing_advisory(self):
        self.assertEqual(self.run_main([self.advisory()], labels=["docs-not-needed"]), 0)
        [(method, _, body)] = self.api.writes()
        self.assertEqual(method, "PATCH")
        self.assertIn("docs-not-needed", body["body"])

    def test_already_resolved_advisory_is_not_rewritten(self):
        resolved = "**Release note advisory resolved**\n\nNo release note is needed: the `docs-not-needed` label is set."
        self.assertEqual(self.run_main([self.advisory(resolved)], labels=["docs-not-needed"]), 0)
        self.assertEqual(self.api.writes(), [])

    def test_openai_failures_skip_the_quality_check(self):
        failures = {
            "dropped connection": http.client.RemoteDisconnected("gone"),
            "truncated read": http.client.IncompleteRead(b""),
            "connection reset": ConnectionResetError(),
            "http error": urllib.error.HTTPError("u", 500, "boom", {}, None),
            "refusal": {"choices": [{"message": {"content": None, "refusal": "no"}}]},
            "list content": {"choices": [{"message": {"content": "[]"}}]},
            "null content": {"choices": [{"message": {"content": "null"}}]},
            "no choices": {"choices": []},
            "not json": {"choices": [{"message": {"content": "nope"}}]},
        }
        for name, failure in failures.items():
            with self.subTest(name):
                self.assertEqual(self.run_main([make_comment(TEMPLATE.format(note=NOTE))], openai=failure), 0)
                self.assertIn("skipping", self.output)

    def test_advisory_write_failure_is_not_fatal(self):
        pr = make_pr()
        api = FakeAPI({
            ("GET", f"{API}/repos/{REPO}/pulls/1"): pr,
            ("GET", pr["comments_url"]): [make_comment(TEMPLATE.format(note=NOTE))],
            ("POST", pr["comments_url"]): ConnectionResetError(),
            ("POST", "https://api.openai.com/"): openai_reply("advise", "Typo."),
        })
        self.assertEqual(self.run_script(check_changelog, {"pull_request": pr}, api, OPENAI_API_KEY="k"), 0)
        self.assertIn("Could not write the advisory comment", self.output)

    def test_comment_fetch_failure_reports_error(self):
        pr = make_pr()
        api = FakeAPI({
            ("GET", f"{API}/repos/{REPO}/pulls/1"): pr,
            ("GET", pr["comments_url"]): http.client.RemoteDisconnected("gone"),
        })
        self.assertEqual(self.run_script(check_changelog, {"pull_request": pr}, api), 1)
        self.assertIn("::error::Could not fetch PR comments", self.output)

    def test_code_span_handles_backticks(self):
        self.assertEqual(check_changelog.code_span("a `b` c"), "``a `b` c``")
        self.assertEqual(check_changelog.code_span("`x`"), "`` `x` ``")


if __name__ == "__main__":
    unittest.main()
