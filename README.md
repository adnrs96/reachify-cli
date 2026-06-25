# reachify-cli

A command-line **judgement-job worker for agents**. It claims jobs from the
Velorify backend, lays their files onto disk, and hands an agent a single file to
read and execute — then reports the agent's output back.

> **Status:** rough wireframe. Surfaces are deliberately minimal.

## Commands

Just three:

| Command | What it does |
| --- | --- |
| `login` | Save credentials to `~/.reachify/.profile`. |
| `get-job` | Claim the next job, write its files, **print the agent file path**. |
| `complete-job <id>` | Read the agent's output and report it to the backend. |

The raw judgement-jobs endpoints (list / claim / get / complete) are internal
plumbing — `get-job` and `complete-job` are the only worker-facing verbs.

## The flow

```
            reachify get-job
                  │  poll queue → claim a job → download/write assets
                  │  render prompt (resolve ${asset:ref} → absolute paths)
                  ▼
        /tmp/.reachify/wk/<job>/agent.md      ← printed to stdout (only this)
                  │
        agent reads agent.md, does the work,
        and (per the prompt's trailing write-command)
        writes its judgement to the predefined output path
                  │
                  ▼
            reachify complete-job <job_id>
                  │  GET job → find answer_path → read judgement
                  ▼
        POST /judgement-jobs/<id>/complete  { answer: <judgement> }
```

`get-job` puts **only the agent file path on stdout** (clean for
`AGENT=$(reachify get-job)`); everything else — claimed job id, asset locations,
the expected output path — goes to stderr. When no job is claimable it prints
nothing and exits 0.

## Auth

Credentials live in `~/.reachify/.profile` (JSON, `0600`):

```json
{
  "id": "worker-1",
  "identity_token": "…",
  "api_base_url": "http://localhost:8000"
}
```

The identity token is sent in the `richefy_api_at` header. The profile `id`
doubles as the default worker id (override per-call with `--worker-id`).

### Backend base URL

Resolution order (first wins):

1. `--api-base-url` saved at login (`Profile.api_base_url`)
2. `REACHIFY_API_BASE_URL` environment variable
3. default `http://localhost:8000`

## Install

`reachify` ships as a **relocatable, self-contained tooling tarball** — it
bundles its own Python interpreter (via [python-build-standalone]), so no Python
is required on the target machine. Extract it anywhere and run.

The plugin (separate repo) installs this automatically at session start; see
[Distribution](#distribution-how-the-plugin-consumes-this). To install by hand:

```bash
VERSION=0.0.1
REACHIFY_VERSION=$VERSION bash scripts/reachify-install.sh   # downloads, verifies, symlinks
reachify --version
```

This downloads `reachify-<version>-<os>-<arch>.tar.gz` from the
[latest release](../../releases/latest), extracts it to
`~/.reachify/tooling/<version>/`, and symlinks `reachify` onto your PATH.

> The bundle is unsigned but **not** subject to macOS Gatekeeper quarantine when
> extracted from a tarball locally (unlike a downloaded Mach-O binary), so there
> is no `xattr` step. Startup is ~0.05 s (loads straight from disk).

### Build a tarball yourself

Requires [`uv`] on the **build** machine only. The bundle is platform-specific:
build on macOS for a macOS bundle, on Linux for a Linux one.

```bash
bash scripts/build-tarball.sh        # → dist/reachify-<version>-<os>-<arch>.tar.gz (+ .sha256)
```

Pushing a `v*` git tag builds all four targets (darwin arm64/x64, linux
arm64/x64) in CI and attaches them to a GitHub Release
(see [.github/workflows/release.yml](.github/workflows/release.yml)).

### From source (machines that have Python)

```bash
uv tool install .                    # or: pipx install .
```

## Distribution: how the plugin consumes this

This repo is the **build side**; a separate Claude Code plugin (skill + hooks) is
the **consume side**. The contract between them:

1. This repo's CI publishes, per release, one tarball per platform plus a
   `.sha256` sidecar. Each tarball extracts to:

   ```
   python/      # bundled standalone CPython (native, relocatable)
   lib/         # reachify + deps (pure-Python, on PYTHONPATH)
   reachify     # POSIX launcher: python/bin/python3 -m reachify, resolves symlinks
   VERSION
   ```

2. The plugin's **session-start hook** runs the logic in
   [scripts/reachify-install.sh](scripts/reachify-install.sh) (copy it into the
   plugin, or curl it from a pinned tag). It:
   - resolves the pinned `REACHIFY_VERSION` + host platform,
   - downloads & sha256-verifies the tarball **only if**
     `~/.reachify/tooling/<version>/` is missing (warm sessions are instant),
   - extracts atomically and symlinks `reachify` onto PATH.

   > ⚠️ The symlink goes into a **conventional command dir** — it tries
   > `/usr/local/bin`, `/opt/homebrew/bin`, `~/.local/bin`, `~/bin` in that
   > order, preferring one already on PATH — never just any writable dir early
   > on PATH (e.g. `node_modules/.bin`). `/usr/bin` is excluded (SIP-protected
   > on macOS). Override with `REACHIFY_BINDIR`. It then repoints any older
   > reachify symlinks and warns if a foreign `reachify` would shadow it.

[python-build-standalone]: https://github.com/astral-sh/python-build-standalone
[`uv`]: https://docs.astral.sh/uv/

## Claude Code plugin

This repo doubles as a **Claude Code plugin** that ships the reachify worker as a
skill (`reachify`) plus a `/reachify:reachify-worker` slash command. The
plugin layout lives alongside the Python package:

```
.claude-plugin/
├── plugin.json          # plugin manifest
└── marketplace.json     # lets the repo install itself as a plugin
skills/
└── reachify/SKILL.md
commands/
└── reachify-worker.md
```

The skill calls the `reachify` binary on your `PATH`, so install the CLI first
(see [Install](#install)).

### Install the plugin

```bash
# Point Claude Code at this repo as a marketplace…
/plugin marketplace add reachify/reachify      # or a local path: /plugin marketplace add ./reachify-cli
# …then install the plugin
/plugin install reachify@reachify
```

Restart Claude Code. The `reachify` skill is now available to the Skill
tool, and `/reachify:reachify-worker` drives one worker tick. Wrap it with
`/loop` to keep draining the queue:

```
/loop 30s /reachify:reachify-worker
```

### Develop the plugin locally

Add the checkout as a marketplace and install — edits to `skills/` and
`commands/` take effect after a restart:

```bash
/plugin marketplace add ./reachify-cli
/plugin install reachify@reachify
```

## Usage

```bash
# One-time
reachify login --id worker-1 --token <token> --api-base-url http://localhost:8000

# Claim work and hand the agent a file to execute
AGENT_FILE=$(reachify get-job --definition-key tone_check)
[ -n "$AGENT_FILE" ] || exit 0          # nothing to do
your-agent run "$AGENT_FILE"            # agent reads it, writes the predefined output

# Report the result the agent produced
reachify complete-job job-x
```

### The agent file

`get-job` writes the job's prompt to `<work_dir>/agent.md` with every
`${asset:<ref>}` placeholder replaced by the asset's absolute on-disk path, so
the file is self-contained. The prompt's predefined trailing instruction tells
the agent where to write its judgement (`workspace.answer_path`).

### Reading output

`complete-job` reads that output file as the answer. A JSON object is sent
as-is; any other JSON value is wrapped under `value`; non-JSON text under `text`.

## Project layout

| File | Responsibility |
| --- | --- |
| `cli.py` | the three commands (`login`, `get-job`, `complete-job`) |
| `profile.py` | read/write `~/.reachify/.profile` |
| `api.py` | authenticated httpx client (`richefy_api_at`, raw JSON) |
| `jobs.py` | judgement-jobs client + asset/prompt/agent-file preparation |
| `models.py` | dataclasses for the job contract (defensive parsing) |
| `config.py` | base URL + profile paths + workspace fallback root |

## Robustness

Job parsing tolerates backend schema drift: unknown fields are ignored but kept
in `Job.raw`; missing or renamed fields degrade to `None` or a sensible fallback
rather than crashing.
