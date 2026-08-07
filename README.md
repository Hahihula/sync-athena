# sync-athena

Bidirectional GitHub ↔ Athena sync, packaged as a reusable composite GitHub
Action. Three modes:

| mode | trigger | effect |
| --- | --- | --- |
| `issue_to_task` | `issues: [opened, edited, reopened]` or `workflow_dispatch` | creates a new `<PREFIX>-NNNN: <title>` task, or updates the existing one if a task with the same `github-issue-<n>` hashtag already exists. Optionally sets editors / co-authors. |
| `comment_to_task` | `issue_comment: [created, edited]` | appends the comment as a `type=comment` child node on the matching task. Resolves issues via the `github-issue-<n>` hashtag and PRs via the `<PREFIX>-NNNN` title. |
| `pr_to_comment` | `pull_request: [opened, reopened, ready_for_review]` | resolves `<PREFIX>-NNNN` from the PR title and posts a `type=comment` child node on the matching task with the PR URL |

The ticket prefix (`EIM`, `ESP`, anything) is a workflow input, so the same
action serves any team that adopts Athena.

## Layout

```
sync-athena/
├── action.yml                        composite action manifest
├── requirements.txt                  httpx, markdown-it-py (both public PyPI)
├── src/sync_athena/
│   ├── __init__.py
│   ├── athena_client.py              thin sync HTTP client
│   ├── markdown_to_blocknote.py      markdown -> BlockNote JSON converter
│   ├── counter.py                    next_ticket_number(), find_task_by_hashtag(), retry-on-duplicate
│   ├── issue_to_task.py              entrypoint: GitHub issue -> Athena task (idempotent)
│   ├── comment_to_task.py            entrypoint: GitHub issue/PR comment -> task child
│   └── pr_to_comment.py              entrypoint: PR title -> comment child
└── examples/
    ├── sync-athena.yml               main workflow (all three modes, triggers split)
    └── athena-pr-link.yml            PR-only workflow
```

## Required secrets and variables

Configure once per consuming repo. Values are repo-specific; the table below
shows placeholders.

| where | name | example value |
| --- | --- | --- |
| secret | `ATHENA_TOKEN` | bot token (scope `nodes:read,nodes:write`) |
| secret | `ATHENA_BASE_URL` | your Athena server URL |
| variable | `ATHENA_TICKET_PREFIX` | `EIM` (or `ESP`, `IDF`, whatever your team uses) |
| variable | `ATHENA_PROJECT_UUID` | `540444ee-a030-43a2-a5f9-1f5a12e3ab2f` |
| variable | `ATHENA_DB_PATH` | `project/proj_<uuid>.db` |
| variable | `ATHENA_EDITORS` (optional) | `alice@x.com,bob@x.com` — editors set on every task this action creates |
| variable | `ATHENA_CO_AUTHORS` (optional) | same shape, co-author role |

Create the bot token once with cookie auth (run interactively on any host
that has the Athena CLI installed — the `athena login` command picks up the
server URL from your `.env`):

```bash
athena login
athena bot create -p "<your project name>" --name github-actions --role member
athena bot token create -p "<your project name>" --bot github-actions \
    --name sync-ci --scopes nodes:read,nodes:write --duration 365d
```

## How the "next ticket number" is computed

Athena has no native ticket numbering — nodes have UUIDs and weblink short
IDs. The action derives the next free number by:

1. `GET /api/nodes/advanced_search?type=task&hashtag=<prefix-lowercased>&_db_path=...` — server-side filter by node type + hashtag.
2. Parse names with `^<PREFIX>-(\d+):` client-side, take the max, `+1`.
3. Retry up to 3 times if a concurrent issue creation stole the number.

Every ticket is tagged with hashtag `<prefix-lowercased>` (plus
`github-issue-<n>`) and named `<PREFIX>-NNNN: <title>`, so the search stays
cheap and the regex stays exact.

## Idempotency — `issue_to_task` updates instead of duplicating

Before creating, `issue_to_task` searches for an existing task carrying the
`github-issue-<n>` hashtag (set by every prior run for the same issue). If
found, the task's `name` and `description` are updated in place via
`PUT /api/nodes/{id}`; the original node UUID and ticket key are preserved
so downstream PR references don't break.

This makes it safe to trigger on `issues: [opened, edited, reopened]` and
`workflow_dispatch` backfills without producing duplicates. If the lookup
returns more than one task the action aborts with `::warning::` (an
invariant violation that should never happen for a single Athena project).

## Comment flow

`comment_to_task` handles both issues and PRs (the `issue_comment` event
fires for both). The lookup is:

- **Issue comments**: `find_task_by_hashtag("github-issue-<n>")`. Fails
  fast — if no task exists for the issue, the comment is dropped with a
  warning.
- **PR comments**: hashtag lookup first (`github-pr-<n>` — set by
  `pr_to_comment` on the parent task), falling back to the PR-title regex
  `^<PREFIX>-(\d+)\s*:` so a PR whose ticket was created manually still
  resolves.

The comment body becomes a `type=comment` child node named
`Comment on issue/PR #<n>`, so the original task description is preserved
while the conversation history accumulates underneath it.

## PR title convention

PR titles must start with `<PREFIX>-NNNN:`:

```
EIM-1234: Add login flow
ESP-42: Fix bootloader reset
```

Anything else is logged and skipped — matches the previous Jira
PR-comment workflow's "no issue key found, skipping" behaviour.

## Idempotency / failure model

- Any Athena API error → `::warning::` + exit 0. Workflow never fails.
- Duplicate ticket number → retried up to 3 times before failing.
- Missing `ATHENA_*` env → `::error::` + exit 1 (configuration error, not a runtime one).
- `gh` missing on the runner → comment/label step logs a warning; the
  Athena write still succeeds.
- `set_collaborators` failures (e.g. unknown user email) → `::warning::` + exit 0.

## Inputs

The composite action exposes these inputs. All Athena secrets are forwarded
verbatim to the underlying Python entrypoint.

| input | required | default | notes |
| --- | --- | --- | --- |
| `mode` | yes | – | `issue_to_task`, `comment_to_task`, or `pr_to_comment` |
| `repo` | yes for `issue_to_task` / `pr_to_comment` | – | `owner/repo` slug for `gh` calls |
| `ticket_prefix` | yes | – | ticket key prefix (e.g. `EIM`, `ESP`). The action refuses to run without it, so a misconfigured repo can't accidentally file tickets in someone else's project. |
| `tasks_folder` | no | `📝 Tasks` | Athena folder name where tickets are filed |
| `author` | no | `github-actions@users.noreply.github.com` | email written into the created nodes |
| `editors` | no | `""` | comma-separated editor emails set on the created task (`issue_to_task` only) |
| `co_authors` | no | `""` | comma-separated co-author emails set on the created task (`issue_to_task` only) |
| `athena_token` | yes (via env) | – | bot token |
| `athena_base_url` | yes (via env) | – | Athena server URL |
| `athena_project_uuid` | yes (via env) | – | project UUID |
| `athena_db_path` | yes (via env) | – | `project/proj_<uuid>.db` |

## Adoption: per-team config

This action started life for the EIM/IM team. Other teams adopting it set
their own `vars.ATHENA_TICKET_PREFIX` and the four Athena-side
secrets/variables; the workflow file stays unchanged.

```yaml
# another-team/.github/workflows/sync-athena.yml
- uses: hahihula/sync-athena@v1
  with:
    mode: issue_to_task
    repo: ${{ github.repository }}
    ticket_prefix: ${{ vars.ATHENA_TICKET_PREFIX }}            # no fallback by design
    athena_project_uuid: ${{ vars.ATHENA_PROJECT_UUID }}
    athena_db_path: ${{ vars.ATHENA_DB_PATH }}
    athena_base_url: ${{ secrets.ATHENA_BASE_URL }}
    athena_token: ${{ secrets.ATHENA_TOKEN }}
```

`ATHENA_TICKET_PREFIX` is intentionally required with no default. If a team
forgets to set it, the action emits `::error::ATHENA_TICKET_PREFIX must be
set…` and exits 1 — far better than silently filing tickets in some other
team's project.
