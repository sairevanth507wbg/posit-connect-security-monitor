# Security — before publishing to a public repository

## The one rule

**Secrets live in `.env`. `.env` is git-ignored. Nothing else holds a real value.**

Everything below exists to enforce that rule and to catch the cases where it
slips.

---

## Enable the pre-commit hook (do this first)

```bash
git config core.hooksPath .githooks
```

One command, per clone. From then on every `git commit` scans staged content and
**blocks the commit** if it finds a credential.

Verify it is active:

```bash
python scripts/check_secrets.py --all
```

Run it manually any time. `--all` scans every tracked file; no arguments scans
staged changes.

### What it catches

| Rule | Example |
|---|---|
| `private-key` | `-----BEGIN RSA PRIVATE KEY---` (truncated) |
| `connect-api-key` | `CONNECT_API_KEY` set to a 32-char hex string |
| `databricks-pat` | a `dapi` prefix followed by 32 hex chars |
| `aws-access-key` | an `AKIA` prefix followed by 16 chars |
| `github-token` | a `ghp_` / `gho_` prefix |
| `slack-token` | an `xoxb-` / `xoxp-` prefix |
| `assigned-secret` | `password` assigned a literal value |
| `postgres-url-with-password` | a `postgresql://` URL with inline credentials |
| `forbidden-file` | staging `.env`, `id_rsa`, `*.pem` |

It does **not** flag `os.getenv("DATABRICKS_TOKEN")`, `SecretStr`, or values that
announce themselves as fixtures (`test-...`, `your-...`, `<placeholder>`).

For a deliberate example in documentation or tests, add a comment marker on the
line and the scanner skips it:

```
example_key = "..."   # check-secrets: ignore
```

It is a safety net, not a guarantee — it cannot spot a secret that looks like
ordinary text.

---

## Pre-publish checklist

Run all four before making a repository public.

```bash
# 1. No secrets in any tracked file
python scripts/check_secrets.py --all

# 2. Confirm .env is NOT tracked (must print nothing)
git ls-files | grep -E "(^|/)\.env$"

# 3. No secret ever existed in history (must print nothing)
git log --all --oneline -- .env

# 4. Review every file you are about to publish
git ls-files
```

---

## `.gitignore` is not retroactive

This is the trap that catches people.

`.gitignore` only prevents files that were **never committed** from being added.
If a secret was committed once — even if you delete it in a later commit — it
stays in git history forever and is visible on a public repo.

Check before publishing:

```bash
git log --all --full-history -- .env
git rev-list --all --objects | grep -i "\.env$"
```

**If either prints anything, the secret is in your history.** Two options:

1. **Publish with no history (simplest, recommended).** Copy the working tree
   into a fresh directory, `git init`, and make one initial commit. You lose
   history but guarantee nothing leaks.
2. **Rewrite history** with [`git-filter-repo`](https://github.com/newren/git-filter-repo).
   Slower, error-prone, and everyone must re-clone.

Either way: **rotate the exposed credential.** Once a secret has been pushed
publicly, assume it is compromised. Scrubbing history does not un-leak it.

---

## Beyond credentials: internal infrastructure

Credentials are the obvious risk. On an internal corporate system these also
matter on a public repo:

- **Internal hostnames** — `posit-connect-qa.example.org`, `w0lxd...` — these are
  reconnaissance data.
- **Content GUIDs, bundle IDs, internal `__api__` URLs.**
- **Colleagues' usernames and local paths** — `/Users/someone/workspace/...`.
- **Database names, schema names, warehouse IDs.**

Posit tooling writes exactly this into deployment records, which is why
`.gitignore` here excludes:

```
.rsconnect/
rsconnect-python/
.posit/publish/deployments/
```

Keep `.posit/publish/<config>.toml` if you want the publish config shared — it
describes *what* to deploy. Exclude `deployments/` — that records *where* it
was deployed, with live URLs.

Check before publishing:

```bash
git ls-files -z | xargs -0 grep -ohEi "[a-z0-9_-]+\.(yourcompany|internal)\.(org|com)" | sort -u
git ls-files -z | xargs -0 grep -ohE "/(Users|home)/[A-Za-z0-9_.-]+" | sort -u
```

---

## If a secret is exposed

1. **Rotate it immediately** — before cleaning up the repository.
   - Posit Connect: user menu → API Keys → delete, then create a new one.
   - PostgreSQL: `ALTER USER <user> WITH PASSWORD '<new>';`
   - Databricks: revoke the PAT or rotate the service-principal secret.
2. Update your local `.env` with the new value.
3. Then clean the repository (see above).
4. Tell your security team. On a corporate system this is usually required, not
   optional.

Rotation first. Cleanup second. A scrubbed repo with a live leaked key is still
a live leaked key.

---

## How this project handles secrets

- `CONNECT_API_KEY` and `POSTGRES_PASSWORD` are `pydantic.SecretStr` — they
  render as `**********` in logs, `repr()`, and tracebacks.
- Loggable configuration goes through `Settings.safe_summary()`, which
  hard-codes `***redacted***`.
- Database URLs render with `hide_password=True` in every log and error message.
- The API key is read only by `Settings.api_key_value()`, called in exactly one
  place: building the HTTP `Authorization` header.
- `.env.example` is committed with placeholders; `.env` never is.
