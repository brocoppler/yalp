# Secrets History Audit — 2026-07-01

**Verdict: `CLEAN`.** No API keys, passwords, or SSH key material were found in
any commit reachable from any ref in this repository. The unpushed local `main`
was verified and pushed to `origin`.

> This document is deliberately value-free. It records *methodology*, *scope*,
> and *per-category verdicts* only. No secret, and no fragment of any secret, is
> reproduced here (or anywhere in the repo). Category patterns below describe the
> *shape* of what was hunted, never a literal credential.

---

## 1. Scope

- **Every commit reachable from any ref**, enumerated with `git rev-list --all`
  (heads, remote-tracking refs, and the concurrent `retinue/*` task branches).
- The history is shared across all git worktrees, so a single object database was
  scanned once and covers every branch.
- The audit ran while sibling task branches were actively committing, so history
  advanced repeatedly during the audit (from 84 up to 90 commits reachable from
  `main`; `main` moved `860e80c → 56734e1 → 0ede5d6 → 80fd0e8 → a04233e`). Each
  newly landed commit
  was re-scanned before the gate opened, and every push was pinned to an exact
  verified tip (see §6) so no unverified commit could ride along. The final all-refs
  sweep of 90 commits was clean; the only occurrences of any hunted keyword outside
  product code are documentation placeholders, test fixtures, and this audit's own
  deliverables (this file plus the `.gitignore` pattern list), none of which are
  secret values.

## 2. Threat categories hunted

| # | Category | Pattern shape (generic) |
|---|----------|-------------------------|
| 1 | Anthropic API keys | the `sk-ant-` prefix; long high-entropy token following it; `ANTHROPIC_API_KEY` assigned a non-placeholder value |
| 2 | The live Anthropic key | exact byte-for-byte value read from the git-ignored `.env` (108 chars) |
| 3 | SSH host password | exact byte-for-byte value read from an out-of-repo password file (12 chars) |
| 4 | Telegram bot tokens | `<digits>:AA<alphanumeric>` token shape |
| 5 | Private key material | `BEGIN … PRIVATE KEY` armor blocks; `id_rsa` / `id_ed25519` blobs; `known_hosts` content |
| 6 | Loose credential keywords | `password`, `sshpass` (triaged: abstract docs vs. literal values) |

The exact secret values (categories 2 and 3) were read only into shell variables
and compared with `git grep -F` / `git log -S`; they were never printed, logged,
or written to any file.

## 3. Methodology (commands, described generically)

**Content scan — full tree of every commit.** For each pattern:

```sh
ALL=$(git rev-list --all)
git grep -I -l -E '<pattern>' $ALL          # lists <commit>:<path>, not the line
git log  --all --oneline -G '<pattern>'     # pickaxe cross-check: commits that changed it
```

For the two exact secret values, `-F` (fixed-string) matching against a shell
variable was used so the literal value never appeared in a command, and only
`<commit>:<path>` (via `-l`) was ever emitted.

> **Tooling note for future auditors:** the login shell here is `zsh`, which does
> **not** word-split an unquoted `$ALL`. An early pass silently passed all SHAs as
> one oversized argument (git error suppressed by `2>/dev/null`), producing false
> "no match" results. Every scan below was re-run under `bash` (correct
> word-splitting) and independently cross-checked with the `git log -G/-S`
> pickaxe, which does not depend on argument splitting. Verify your scan harness
> against a control term you *know* is present before trusting any negative.

**Filename scan — every path ever added:**

```sh
git log --all --diff-filter=A --name-only --format= | sort -u
```

The unique add-list was screened for `.env` (bare), `*.pem`, `*.key`, `*.p12`,
`*.pfx`, `*.ppk`, `id_rsa*`, `id_ed25519*`, `known_hosts`, and `ssh-izzy*`.

## 4. Per-category verdicts

| Category | Result | Notes |
|----------|--------|-------|
| Live Anthropic key (exact) | **CLEAN** | 0 occurrences in any commit. |
| SSH host password (exact) | **CLEAN** | 0 occurrences in any commit. |
| `sk-ant-` real-length keys | **CLEAN** | 0 occurrences. |
| `sk-ant-` short placeholders | **Benign** | Only documentation placeholders and one test fixture (obvious non-secrets in `SETUP.md`, `.env.example`, `src/yalp/deliberative/vision.py`, `tests/test_vision.py`). |
| `ANTHROPIC_API_KEY` assignments | **Benign** | Only empty (`.env.example` template) or placeholder assignments; never a real value. |
| Telegram bot tokens | **CLEAN** | 0 occurrences. |
| `BEGIN … PRIVATE KEY` blocks | **CLEAN** | 0 occurrences. |
| `id_rsa` / `id_ed25519` blobs | **CLEAN** | Only one doc line referencing an `id_ed25519.pub` *path* in an `ssh-copy-id` instruction; no key bytes. |
| `known_hosts` / `sshpass` | **CLEAN** | 0 occurrences. |
| `password` keyword | **Benign** | Only Raspberry-Pi-Imager setup prose ("pick a real password — write it down", "SSID + password", "no password required"); no literal credentials. |
| Sensitive filenames ever added | **CLEAN** | Only `.env.example` (a committed, value-free template) matched; no bare `.env`, cert, private-key, or `ssh-izzy*` file was ever committed. |

**Untracked-secret claims verified:**

- `.env` (holding the live key) is matched by `.gitignore` and is **not** tracked
  by git — confirmed with `git check-ignore` and `git ls-files`.
- The SSH password file lives **outside** the repository tree.

## 5. `.gitignore` hardening

The Secrets section was expanded from a single `.env` line to cover the full
credential surface, while preserving the committed template:

- `.env`, `.env.*`, with `!.env.example` re-included;
- `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.ppk`;
- `id_rsa`, `id_rsa.*`, `id_ed25519`, `id_ed25519.*`, `known_hosts`;
- `ssh-izzy*`.

Ignore/allow behavior was validated with `git check-ignore`: every credential
pattern is ignored; `.env.example` and normal source/docs remain committable.

## 6. Push decision

Because **every** scan came back clean, the gate opened. `main` was pushed to
`origin/main` in verified, pinned increments as the orchestrator integrated
sibling work (`fb16da6..0ede5d6`, then `..80fd0e8`, then `..a04233e`), each push
targeting an exact reviewed commit — never the moving `main` ref — so no
unverified commit could ride along. Last verified state at time of writing:
`origin/main == local main == a04233e7daaaed67f64b7cea1620b1e9a96b98a9`, 0
unpushed, and all sibling branches level with `main`.

This gate is **per-commit and continuous**: any commit that lands on `main`
*after* `a04233e` must re-pass the same scan before it is pushed. The durable
deliverable here is the methodology (§3) and the hardened `.gitignore` (§5), not
a single frozen SHA.
