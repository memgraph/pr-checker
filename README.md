# pr-checker

A GitHub Action that checks pull request labels, the milestone, and change-log items.

It runs two checks:

1. **Labels and milestone.** The PR must have:
   - exactly one docs label (`docs-labels`),
   - at least one component label (`component-labels`), if configured,
   - at least one type label (`type-labels`),
   - a milestone (`require-milestone`).
2. **Release note.** A PR comment must contain the PR template's `### Documentation checklist`
   section, with a filled-in sub-bullet under `- [ ] Write a release note`. Only comments by
   the PR author, or by a repo owner, member or collaborator, count. PRs with the
   `skip-release-note-label` label (default `docs-not-needed`) skip this check.

   If `openai-api-key` is set, the note is also sent to an LLM for a quality check:
   - `pass`: an earlier advisory comment, if any, is marked as resolved.
   - `advise`: an advisory comment is posted on the PR, and the check passes.
   - `block`: an advisory comment is posted on the PR, and the check fails.

   If the key is missing or the API call fails, the quality check is skipped.

The PR's labels, milestone and comments are fetched from the API when the check runs, so a
re-run sees the PR as it is now, not as it was when the original event fired. If fetching the
PR fails, the check logs a warning and uses the event payload instead, which can be stale on a
re-run.

## Usage

```yaml
name: Pre-merge checks

on:
  pull_request:
    types: [opened, reopened, synchronize, labeled, unlabeled, milestoned, demilestoned]

jobs:
  pr-checker:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: memgraph/pr-checker@<commit-sha> # v1
        with:
          github-token: ${{ github.token }}
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          component-labels: memgraph,memgraph-ha,memgraph-lab,memgraph-operator,infrastructure
```

The workflow should trigger on `labeled`, `unlabeled`, `milestoned` and `demilestoned`, so the
check re-runs when those change. You don't need `actions/checkout`: the action reads only the
event payload and the GitHub API.

### Re-running after editing the release note

The release note lives in a PR comment, and adding or editing a comment doesn't trigger the
check. After changing the note, re-trigger the check with any event that does run it: push a
commit, add or remove a label, or change the milestone. You can also re-run the failed job
from the PR's **Checks** tab. A re-run fetches the PR's labels and comments when it runs, so it
sees the edited note.

Don't add an `issue_comment` trigger to work around this: it would start a workflow run for
every comment on every issue and PR in the repo.

### Merge queue

On `merge_group` events the action does nothing and passes, since the labels and release note
were already checked on the PR. Add `merge_group` to the triggers if you make this a required
check and use a merge queue.

### Fork PRs

On `pull_request` events from forks, `github.token` is read-only. The checks still run, but
the advisory comment can't be posted.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `github-token` | *(required)* | Token used to read the PR and its comments, and post the advisory comment. Use `github.token` or a GitHub App token: an advisory comment is only recognised, and later updated, if a bot posted it. |
| `check-labels` | `true` | Run the label and milestone check. |
| `check-release-note` | `true` | Run the release-note check. |
| `docs-labels` | `docs-changelog-only,docs-needed,docs-not-needed` | Exactly one is required. Empty disables this check. |
| `component-labels` | *(empty)* | At least one is required. Empty disables this check. |
| `type-labels` | `bug,feature,infrastructure` | At least one is required. Empty disables this check. |
| `require-milestone` | `true` | Require a milestone. |
| `skip-release-note-label` | `docs-not-needed` | PRs with this label skip the release-note check. |
| `openai-api-key` | *(empty)* | Enables the LLM quality check. |
| `openai-model` | `gpt-5-mini` | Model used for the quality check. |
| `python-version` | `3.12` | Python version to set up (3.10 or newer). |

## PR template

The release-note check expects a PR comment containing this section:

```markdown
### Documentation checklist
- [ ] Write a release note, including added/changed clauses
    - What has changed? What does it mean for a user? What should a user do with it? [#{{PR_number}}]({{link to the PR}})
```

The placeholder sub-bullet doesn't count, with or without its link. Replace it with the real
note, e.g. `Added remote monitoring options. [#123](https://github.com/memgraph/helm-charts/pull/123)`.
A note can wrap onto indented continuation lines; they're joined into one line. The first
filled-in sub-bullet is the note.

## Development

The scripts use only the Python standard library. Run the tests with:

```sh
python -m unittest discover -s tests -v
```

To run the label check locally, outside a PR event:

```sh
PR_LABELS="docs-needed,bug" PR_MILESTONE="v1" python scripts/check_labels.py
```

## Releasing

Tag a release (`v1.0.0`) and move the major tag (`v1`) to it. Consumers should pin to the
commit SHA, with the tag in a comment.
