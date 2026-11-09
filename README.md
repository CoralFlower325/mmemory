<div align="center">

# mmemory

**A local, five-field memory layer for AI-assisted work.**

Store durable lessons in SQLite, find them with CJK-aware FTS5, and keep every project isolated.

[English](README.md) · [简体中文](README.zh-CN.md)

[![CI](https://github.com/CoralFlower325/mmemory/actions/workflows/ci.yml/badge.svg)](https://github.com/CoralFlower325/mmemory/actions/workflows/ci.yml) ![Python](https://img.shields.io/badge/Python-%3E%3D3.10-3776ab) ![SQLite](https://img.shields.io/badge/SQLite-FTS5-003b57) [![MIT](https://img.shields.io/badge/license-MIT-0969da)](LICENSE)

[Quick start](#quick-start) · [How recall works](#how-recall-works) · [CLI](#cli) · [Privacy](#data-and-privacy) · [Contributing](CONTRIBUTING.md)

</div>

## What It Does

mmemory is a small command-line memory system for work that spans chats, coding
sessions, documents, and experiments. A memory is five short fields:

| Field | Purpose |
| --- | --- |
| `goal` | The task or fact being remembered |
| `approach` | What was tried |
| `outcome` | The current result |
| `success_key` | What made it work |
| `pitfall` | The failure signal and its resolution |

Writing, tagging, and ordinary recall do not call an LLM or start a background
service. Python rules and `jieba` extract fixed tags and keywords, while SQLite
FTS5 and bounded text matching find candidates inside one explicit project
namespace.

| Keep | Find | Maintain |
| --- | --- | --- |
| Five-field records with type, source, verification, and validity metadata | Project-scoped CJK-aware FTS5, aliases, tags, and literal-text recall | Revision, protection, decay, archive, deep recall, hard purge, and merge workflows |
| Exact de-duplication and milestone updates | Recall explanations and bounded context blocks | Read-only review clues and SQLite-consistent backups |
| Markdown export for Obsidian | Optional reinforcement only after real use | Synthetic temporary databases for every test |

## Quick Start

Requirements:

- Python 3.10 or newer
- SQLite built with FTS5
- `jieba==0.42.1`

```bash
git clone https://github.com/CoralFlower325/mmemory.git
cd mmemory
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cli.py --help
```

The default database is `memory.db` in the project directory. Keep it private.
Use `MMEMORY_DB` to select another file:

```bash
MMEMORY_DB=/absolute/path/to/private-memory.db .venv/bin/python cli.py stats
```

### Save And Recall A Memory

```bash
.venv/bin/python cli.py store \
  --goal "mmemory recall regression" \
  --approach "Compare literal hits before tag ranking" \
  --outcome "Generic action words no longer pull unrelated memories" \
  --success-key "Require a real content match" \
  --pitfall "Tag-only candidates appeared when the query was checked by rank alone; filter candidates by content first" \
  --kind experience \
  --verification verified \
  --source-ref docs/recall-notes.md \
  --project-key mmemory

.venv/bin/python cli.py recall "mmemory recall regression" --explain
.venv/bin/python cli.py list --project-key mmemory
```

`goal` is always required. Experiences also require `approach` and `outcome`;
project facts require `outcome`; preferences may use `goal` alone. All five fields
together are limited to 1000 characters.

## Project Namespaces

Project isolation is a hard boundary. Recall never widens to another project
unless the query resolves to more than one explicit project or you omit the
project scope intentionally.

mmemory ships with a few neutral examples. Add your own write and query aliases
by copying [`projects.example.json`](projects.example.json) to `projects.json`:

```json
{
  "projects": {
    "my-project": {
      "write_aliases": ["my-project", "project codename"],
      "query_aliases": ["my-project", "project topic"]
    }
  }
}
```

`projects.json` is ignored by Git. Point `MMEMORY_PROJECTS_FILE` at another
private file when you keep configuration outside the repository.

For domain aliases, copy [`synonyms.example.json`](synonyms.example.json) to
`synonyms.json` or set `MMEMORY_SYNONYMS_FILE`. Each array is bidirectional.

If a project has no reliable alias, use the same explicit `--project-key` for
writes and recall. Use `--project-key ""` to select the shared bucket.

## How Recall Works

Recall combines four layers inside the resolved project:

1. A jieba-pretokenized `search_text` column backed by FTS5.
2. Literal matches across the five fields.
3. Keyword tags generated from the stored text.
4. Query aliases, ASCII word boundaries, and identifier-aware splitting.

Tags and broad action words such as "fix", "configure", or "status" can affect
ordering but cannot pull a candidate into the result by themselves. A candidate
must have a real content hit.

Use `--explain` to inspect the resolved project, required terms, source of each
hit, and ordering reason. The default injected summary contains the goal,
outcome, and one lesson, with wrapper text included in a 1200-character budget.

Relations affect recall: a target of a `supersedes` relation is hidden by
default. `--include-superseded` restores it with a label. `--deep` considers
archived records after current hits, and `--include-expired` shows records outside
their validity window. Memory text is presented as data, not as instructions.

Ordinary recall does not change use counts or weights. Add `--reinforce` only
after the memory was actually used. If a new result changes a known fact, use
`update ID` rather than storing another phrasing of the same fact.

## Metadata And History

Memory type, provenance, verification, and validity are persisted:

```bash
.venv/bin/python cli.py store \
  --goal "Production API listens on port 9090" \
  --approach "Checked the deployed configuration" \
  --outcome "Port changed from 8080 to 9090" \
  --kind project_fact \
  --verification verified \
  --source-ref deploy/config.yaml \
  --valid-from "2026-11-01" \
  --project-key api-service

.venv/bin/python cli.py update 42 --outcome "Port is 9090" --reason "Deployment verified"
.venv/bin/python cli.py history 42
```

`kind` is `experience`, `project_fact`, or `preference`. Verification is
`unverified`, `verified`, or `disputed`. Updates and lifecycle changes retain
snapshots in `memory_revisions`; the default limit is 20 retained revisions per
memory and can be changed with `MMEMORY_REVISION_LIMIT`. Preferences decay more
slowly than experiences and project facts.

## CLI

| Command | What it does |
| --- | --- |
| `store` | Add a five-field memory or validate an imported conversation |
| `recall` | Search one project; supports `--explain`, `--deep`, validity, supersession, and explicit reinforcement |
| `update ID` | Revise text while preserving project and usage metadata |
| `history ID --project-key ...` | Inspect the current row and prior snapshots inside one project scope |
| `list` | Inspect active, archived, or all memories |
| `protect` / `unprotect` | Exempt or resume decay by explicit ID and reason |
| `archive` / `restore` | Transition memory status without changing its ID |
| `review` | Read-only duplicate, conflict, and mixed-project clues |
| `link` / `relations --project-key ...` | Record and inspect `supersedes` or `derived_from` evidence within one project scope |
| `decay` | Preview or apply elapsed-time decay and threshold archival |
| `dream` | Produce manual merge candidates; never edits automatically |
| `reindex` | Rebuild jieba tokens and the FTS5 index |
| `purge` | Irreversibly delete a record, its relations, and revision snapshots |
| `backup` | Create a consistent SQLite snapshot without overwriting |
| `stats` | Show counts, kinds, verification states, expiry, unused records, and supersession health |

Run any command with `--help` for its exact arguments.

## Maintenance Model

mmemory separates three time concepts:

- `last_used` is a real use event.
- `decay_cursor_at` is the point through which decay has already been charged.
- `archived_at` records when a memory left the active set.

Repeated dry runs do not charge the same elapsed period twice. Protected records
skip decay. Archiving and restoration keep the original ID; archived time is not
charged retroactively after restore.

Existing schema v1 databases are upgraded in a write-locked transaction on first
open. The migration preserves IDs, adds v2 metadata and revisions, and rebuilds
the derived search index. Use `reindex` after external SQL edits.

`review` is evidence, not a semantic judge. `dream` is a manual dry run. Merge
application is atomic, rejects groups containing protected sources, and records
derived-from relations instead of silently deleting history. `purge` is the
separate irreversible path for sensitive content; an owned connection enables
secure deletion, checkpointing, and `VACUUM`.

Before maintaining a real database, create a consistent snapshot:

```bash
.venv/bin/python cli.py backup --output backups/before-maintenance.db
```

Backups contain private memory text. They are ignored by Git.

## Obsidian Export

Export active memories as Markdown:

```bash
.venv/bin/python obsidian_export.py
```

The default destination is `./obsidian-export`. Override it with
`MMEMORY_OBSIDIAN_DIR`. A destination must be empty or already carry
`.mmemory-export.json`; mmemory refuses to take over an unmarked nonempty
directory. Export is one-way: mmemory writes IDs into generated files and cleans
only files carrying its own marker.

## Data And Privacy

This is a local tool for one trusted operator. It is not a multi-user service
and has no network synchronization.

- Keep `memory.db`, SQLite WAL/SHM files, backups, exports, and local project
  configuration outside source control.
- Tests create temporary synthetic databases. They do not copy production text.
- The public repository intentionally excludes private runtime data, historical
  maintenance records, and installed-host integrations.
- Review logs and screenshots before sharing them; memory text can contain paths,
  names, account details, and project context.

## Development

Run the complete public test suite:

```bash
.venv/bin/python run_tests.py
```

The runner gives every test subprocess a temporary `MMEMORY_DB`, checks exit
codes, and rejects empty or contradictory output. See [`AGENTS.md`](AGENTS.md)
for the storage, transaction, recall, and maintenance contracts.

Repository map:

| Path | Responsibility |
| --- | --- |
| `engine.py` | Database, storage, recall, lifecycle, and backup |
| `tagger.py` | Fixed tags, search tokens, aliases, and configurable equivalent terms |
| `filter.py` | Validation, quality checks, and milestone replacement |
| `dream.py` | Manual merge candidates and atomic application |
| `review.py` | Read-only review clues |
| `obsidian_export.py` | Deterministic Markdown export |
| `cli.py` | Command-line surface |
| `schema.sql` | Schema v2 tables, revision history, FTS5 index, and triggers |

Contributions should keep the public test suite synthetic, focused, and
reproducible. Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md)
before opening a pull request or sharing diagnostics.

## License

[MIT](LICENSE).
