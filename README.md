# sync-athena

Bidirectional GitHub ↔ Athena sync, packaged as a reusable composite GitHub
Action. Three modes:

| mode | trigger | effect |
| --- | --- | --- |
| `issue_to_task` | `issues: [opened, edited, reopened]` or `workflow_dispatch` | creates `<PREFIX>-<issue number>: <title>` as a task, or updates it if it already exists. Adds editors / co-authors. |
| `comment_to_task` | `issue_comment: [created, edited]` | mirrors the comment as a `description` child node under the task, keyed by GitHub comment id so edits update rather than duplicate |
| `pr_to_comment` | `pull_request: [opened, reopened, ready_for_review]` | resolves the task from the PR title key or a `#NNNN` issue reference, and posts a `solution` child node linking to the PR |

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
│   ├── tickets.py                    ticket keys, GitHub ref parsing, duplicate detection
│   ├── collaborators.py              additive, verified editor / co-author assignment
│   ├── issue_to_task.py              entrypoint: GitHub issue -> Athena task (idempotent)
│   ├── comment_to_task.py            entrypoint: GitHub issue/PR comment -> task child
│   └── pr_to_comment.py              entrypoint: PR -> solution child linking the PR
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

## The ticket number is the GitHub issue number

`github.com/espressif/idf-im-ui/issues/1044` is always `EIM-1044`. The
number is read from the issue's `html_url` (falling back to the payload's
`number` field), so a key can be derived from the URL alone, is stable
across re-runs, and cannot be raced by two issues opened at the same time.

Every ticket is named `<PREFIX>-<n>: <title>` and tagged with hashtags
`<prefix-lowercased>` and `github-issue-<n>`.

## Duplicate prevention

Duplicates were caused by relying on hashtag search to find the existing
task: if the post-creation `PUT .../hashtags` call had failed, the search
found nothing and the next run filed a second ticket.

Lookup is now tree-first:

1. list the children of the tasks folder and match on the `<PREFIX>-<n>`
   name — this reads the tree itself, so no index or hashtag write can
   hide an existing task;
2. fall back to the `github-issue-<n>` hashtag search for tasks somebody
   moved out of the folder.

On a hit, `name` and `description` are updated in place via
`PUT /api/nodes/{id}` — the node UUID and ticket key are preserved, so
downstream PR references don't break — and any missing hashtags are
repaired. An ambiguous lookup uses the first match and warns; it never
creates another task.

The child nodes are deduplicated the same way: PR links are keyed by
`PR #<n>` and comments by `Comment #<github comment id>`, so the three
`pull_request` trigger types can't stack up three copies of the same link.

## Child nodes: comments and PR links

Athena has no comment or discussion node type, so both flows create child
nodes under the task:

- **Comments** (`comment_to_task`) become `description` children named
  `Comment #<comment id> on issue #<n>`. Re-running for an edited comment
  updates that node instead of appending a copy.
- **PR links** (`pr_to_comment`) become `solution` children named
  `PR #<n>: <title>` — a PR is the proposed answer to the ticket.

The task description keeps the issue body; the conversation accumulates
underneath it.

## How a PR finds its ticket

First match wins:

1. a `<PREFIX>-NNNN` key anywhere in the PR title — `EIM-1044: Add login flow`;
2. a `#NNNN` issue reference in the title or body — since the ticket number
   *is* the issue number, `Closes #1044` resolves to `EIM-1044`;
3. a `<PREFIX>-NNNN` key anywhere in the PR body.

A PR matching none of these is logged and skipped.

## Leave `author` empty

Athena stores the `author` field of a node **verbatim** as you send it, and
then checks the *caller* against that stored string on every later write.
So an invented value locks the action out of the node it just created:

```
403 You can only edit tags on nodes you created. This node was created by 'EimGitBot'.
```

`EimGitBot` there is not the server rejecting an unknown user — it is the
string the workflow itself sent. The same happens with a bot's display name
or a token's name. A bot's real identity is
`bot-<6 hex project prefix>-<bot name>@bots.local`, which is not something
a workflow should be guessing at.

Omit `author` (the default) and the server fills in whoever the token is.
The action logs it on first creation:

```
::notice::Athena recorded the task author as 'bot-7702e9-github-actions@bots.local'
```

Bearer tokens have no whoami endpoint — `/api/auth/check` only understands
cookie sessions — so reading it back off a created node is the only way to
learn the bot's identity.

## Editors and co-authors

`PUT /api/nodes/{id}/collaborators` replaces both lists wholesale, requires
`apply_to_descendants` in the body, and rejects the whole request if the
node's own author appears in either list. So the action reads the current
lists, merges the configured entries in, drops the author, writes, and
reads back to confirm. Existing collaborators added by hand in the UI are
never dropped, and a user named in both roles ends up a co-author (the
stronger role).

Three things that make an assignment fail, each with its own warning:

- **the token can't manage the node** — only the node author and project
  admins can, so a non-empty `author:` input causes this (see above).
- **the address isn't a known Athena user** or isn't a member of the project.
- **the address is the node's own author** — skipped with a `::notice::`
  rather than failing the request.

Bare usernames are expanded against the optional `email_domain` input, so
`petr.gadorek` and `petr.gadorek@espressif.com` behave the same.

## Idempotency / failure model

- Any Athena API error → `::warning::` + exit 0. Workflow never fails.
- Existing task found → updated in place, never duplicated.
- Missing `ATHENA_*` env → `::error::` + exit 1 (configuration error, not a runtime one).
- `gh` missing on the runner → comment/label step logs a warning; the
  Athena write still succeeds.
- Collaborator failures → `::warning::` naming the address and reason; the
  ticket is still created.

## Inputs

The composite action exposes these inputs. All Athena secrets are forwarded
verbatim to the underlying Python entrypoint.

| input | required | default | notes |
| --- | --- | --- | --- |
| `mode` | yes | – | `issue_to_task`, `comment_to_task`, or `pr_to_comment` |
| `repo` | yes for `issue_to_task` / `pr_to_comment` | – | `owner/repo` slug for `gh` calls |
| `ticket_prefix` | yes | – | ticket key prefix (e.g. `EIM`, `ESP`). The action refuses to run without it, so a misconfigured repo can't accidentally file tickets in someone else's project. |
| `tasks_folder` | no | `📝 Tasks` | Athena folder name where tickets are filed |
| `author` | no | `""` | **leave empty.** Stored verbatim as the node author; any value that isn't the token's own identity causes 403 on every later write. |
| `editors` | no | `""` | comma-separated editor emails added to the task (`issue_to_task` only) |
| `co_authors` | no | `""` | comma-separated co-author emails added to the task (`issue_to_task` only) |
| `email_domain` | no | `""` | expands bare usernames in `editors` / `co_authors` (e.g. `espressif.com`) |
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
