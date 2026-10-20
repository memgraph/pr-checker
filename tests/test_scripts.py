"""Unit tests for the PR checker scripts. Run with: python -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_changelog  # noqa: E402
import check_labels  # noqa: E402
from _common import PLACEHOLDER_TEXT, find_release_note, next_page_url, parse_bool, parse_list  # noqa: E402

TEMPLATE = """\
### Documentation checklist
- [x] Write a release note, including added/changed clauses
    - {note}
- [ ] **[ Documentation PR link memgraph/documentation#XXXX ]**
"""


class FindReleaseNoteTest(unittest.TestCase):
    def test_filled_note(self):
        note = "Added a thing. [#1](https://github.com/o/r/pull/1)"
        self.assertEqual(find_release_note(TEMPLATE.format(note=note)), note)

    def test_placeholder_is_ignored(self):
        self.assertIsNone(find_release_note(TEMPLATE.format(note=PLACEHOLDER_TEXT)))

    def test_missing_anchor(self):
        self.assertIsNone(find_release_note("- [x] Write a release note\n    - Something."))

    def test_unindented_line_ends_section(self):
        body = "### Documentation checklist\n- [x] Write a release note\n- [ ] Other item\n"
        self.assertIsNone(find_release_note(body))


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


class CheckLabelsTest(unittest.TestCase):
    def run_main(self, labels, milestone, **env):
        pr = {"labels": [{"name": n} for n in labels], "milestone": {"title": milestone} if milestone else None}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"pull_request": pr}, f)
        self.addCleanup(os.unlink, f.name)
        base_env = {
            "GITHUB_EVENT_PATH": f.name,
            "DOCS_LABELS": "docs-needed,docs-not-needed",
            "COMPONENT_LABELS": "",
            "TYPE_LABELS": "bug,feature",
            "REQUIRE_MILESTONE": "true",
        }
        with mock.patch.dict(os.environ, {**base_env, **env}, clear=True), \
                mock.patch("builtins.print"):
            return check_labels.main()

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


class CheckChangelogTest(unittest.TestCase):
    def test_has_skip_label(self):
        pr = {"labels": [{"name": "docs-not-needed"}]}
        self.assertTrue(check_changelog.has_skip_label(pr, "docs-not-needed"))
        self.assertFalse(check_changelog.has_skip_label(pr, "other"))
        self.assertFalse(check_changelog.has_skip_label(pr, ""))


if __name__ == "__main__":
    unittest.main()
