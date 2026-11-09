# Security

mmemory stores private text in a local SQLite database. It has no network
service, account system, or multi-user isolation boundary.

## Data Handling

- Keep `memory.db`, WAL/SHM files, backups, exports, and `projects.json` private.
- Recall and export can reveal paths, names, account details, and project context.
- Review logs, terminal output, and screenshots before sharing them.
- Run tests against temporary synthetic databases, never a production memory file.

The repository ignores common runtime data, but `.gitignore` is not a substitute
for checking staged changes before publishing.

## Reporting

Use GitHub's private vulnerability reporting for this repository. Include a
minimal reproduction, affected version or commit, impact, and redacted
diagnostics.

Do not post credentials, private memory text, live account details, or personal
filesystem paths in a public issue. If a credential was exposed, revoke or rotate
it at the provider first.

The project is maintained on a best-effort basis without a guaranteed response
time.
