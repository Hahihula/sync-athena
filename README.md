# sync-athena

Bidirectional GitHub ↔ Athena sync, packaged as a reusable composite GitHub
Action. Two modes:

| mode | trigger | effect |
| --- | --- | --- |
| `issue_to_task` | `issues: opened` or `workflow_dispatch` | files a new `<PREFIX>-NNNN: <title>` task in Athena, comments + labels the GitHub issue with the new key |
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
│   ├── counter.py                    next_ticket_number() + retry-on-duplicate
│   ├── issue_to_task.py              entrypoint: GitHub issue -> Athena task
│   └── pr_to_comment.py              entrypoint: PR title -> comment child
└── examples/
    ├── sync-athena.yml               main workflow (both modes, triggers split)
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

## Inputs

The composite action exposes these inputs. All Athena secrets are forwarded
verbatim to the underlying Python entrypoint.

| input | required | default | notes |
| --- | --- | --- | --- |
| `mode` | yes | – | `issue_to_task` or `pr_to_comment` |
| `repo` | yes | – | `owner/repo` slug for `gh` calls |
| `ticket_prefix` | yes | – | ticket key prefix (e.g. `EIM`, `ESP`). The action refuses to run without it, so a misconfigured repo can't accidentally file tickets in someone else's project. |
| `tasks_folder` | no | `📝 Tasks` | Athena folder name where tickets are filed |
| `author` | no | `github-actions@users.noreply.github.com` | email written into the created nodes |
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