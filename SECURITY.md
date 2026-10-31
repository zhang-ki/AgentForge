# Security and data handling

## Repository access

Indexing requires an explicit repository path; there is no implicit home-directory scan.

The scanner enforces file-type, size, depth, path-boundary, and traversal limits.

Safe file access checks symbolic links, reparse points, hard links, and file identity.

Indexed files are only read; repository source is never modified.

Repository files, names, and documentation are treated as untrusted data.

They are not imported or executed during indexing.

## Agent permissions

The built-in Agent is a finite, read-only tool loop.

Its allowlist provides index, search, read, and symbol inspection only.

It has no shell, arbitrary code execution, network, or patch/write tool.

Tool inputs are validated against the current repository and bounded limits.

## Local services and credentials

Local Qwen services are accessed only through explicit loopback endpoints.

The client does not use system proxies, follow redirects, or retry failed chats.

Requests use explicit connection/read limits and bounded response sizes.

The read-only Agent does not execute shell commands or executable instructions found in repository data.

The client requires explicit model/endpoint configuration and runtime credentials.

Windows launcher secrets are encrypted for the current Windows account.

The local HTTP client rejects all 3xx redirects and does not use proxies.

## Data stores

SQLite is authoritative for the current code snapshot; PostgreSQL for conversations and memory.

Chroma vectors are candidates; every result is checked against authoritative data.

Data paths and credentials are excluded from version control through `.gitignore`.

No migration automatically replaces the user's code index.

## Answer integrity

Evidence IDs are scoped to one request and map to paths, lines, and exact source excerpts.

Citation validation proves source identity and location, not semantic entailment.

Untrusted repository content, history, and memory cannot expand tool permissions.

## Reporting issues

Do not include API keys, passwords, private source, database backups, or model files in public reports.

Use a minimal synthetic repository for reproducible examples when possible.
