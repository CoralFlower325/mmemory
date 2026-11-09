# Contributing

Thanks for helping improve mmemory.

## Local Development

Use Python 3.10 or newer and a SQLite build with FTS5.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cli.py --help
.venv/bin/python run_tests.py
```

Tests must use synthetic temporary databases. Do not copy, inspect, or commit a
real memory database. The test runner sets `MMEMORY_DB` for each subprocess.

Schema changes must preserve old IDs and append columns in migration order.
Search changes must keep the `search_text` field and FTS index rebuild path in
sync. Changes to recall relations should cover both default and explicit history
views.

Before submitting:

```bash
.venv/bin/python run_tests.py
```

Add focused tests for changed behavior. New behavior should use a hard assertion
and a non-zero failure exit code rather than printing an observation as proof.

## Data Boundaries

Never commit:

- `memory.db`, `*.db`, SQLite WAL/SHM files, or backups
- `projects.json` or another private alias configuration
- `synonyms.json` when it contains private terminology
- Obsidian exports, maintenance records, logs, or screenshots containing real text
- credentials, private paths, account details, or installed-host source

Use neutral project names and synthetic five-field records in examples and tests.

## Pull Requests

Keep changes focused. Explain the problem, resulting behavior, compatibility
impact, and verification performed.

Update both [README.md](README.md) and [README.zh-CN.md](README.zh-CN.md) when user
behavior or setup changes. Preserve existing storage and transaction contracts
unless the change explicitly requires a migration.

Report suspected vulnerabilities through the process in [SECURITY.md](SECURITY.md),
not a public issue containing credentials or private memory text.

Contributions are accepted under the repository's [MIT License](LICENSE).
