"""Reachify CLI entry point.

A judgement-job worker for agents. Three commands:

    login         save credentials to ~/.reachify/.profile
    get-job       claim the next job, lay down its files, print the agent file path
    complete-job  read the agent's output and report it to the backend

`get-job` and `complete-job` are abstractions over the judgement-jobs API — the
raw list/claim/get/complete calls are not exposed directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from . import __version__
from .api import ApiError, ReachifyClient
from .jobs import (
    JobAssetError,
    JobsClient,
    job_id_from_agent_file,
    load_meta,
    prepare_job,
)
from .models import Profile
from .profile import ProfileError, load_profile, save_profile


@click.group()
@click.version_option(__version__, prog_name="reachify")
def main() -> None:
    """Reachify — work the judgement-jobs queue as an agent."""


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------


@main.command()
@click.option("--id", "identity_id", required=True, help="Identity / worker id.")
@click.option("--token", "identity_token", required=True, help="Identity token.")
@click.option("--api-base-url", default=None, help="Override backend base URL.")
def login(identity_id: str, identity_token: str, api_base_url: str | None) -> None:
    """Write credentials to ~/.reachify/.profile."""
    save_profile(
        Profile(id=identity_id, identity_token=identity_token, api_base_url=api_base_url)
    )
    click.echo(f"Saved profile for id={identity_id}.", err=True)


# --------------------------------------------------------------------------
# get-job: claim + prepare, print the agent file path
# --------------------------------------------------------------------------


@main.command("get-job")
@click.option("--platform", default=None, help="Only consider this platform.")
@click.option("--anchor-type", default=None, help="Only consider this anchor type.")
@click.option("--definition-key", default=None, help="Only consider this definition.")
@click.option("--anchor-id", default=None, help="Only consider this anchor id.")
@click.option("--worker-id", default=None, help="Worker id (defaults to profile id).")
@click.option("--lease-seconds", type=int, default=None, help="Requested lease.")
def get_job(
    platform: str | None,
    anchor_type: str | None,
    definition_key: str | None,
    anchor_id: str | None,
    worker_id: str | None,
    lease_seconds: int | None,
) -> None:
    """Claim the next available job and print the agent file path to stdout.

    Internally: poll the queue, claim a job (skipping any lost to other
    workers), download/write its assets, and render the prompt — with every
    ``${asset:<ref>}`` resolved to an absolute path — into the agent file. The
    prompt's predefined trailing write-command tells the agent where to put its
    output. Prints ``None`` (exit 0) when no job is available.
    """
    profile = _require_profile()
    wid = worker_id or profile.id

    with ReachifyClient(profile) as client:
        jobs_api = JobsClient(client)
        candidates = jobs_api.list_jobs(
            platform=platform,
            anchor_type=anchor_type,
            definition_key=definition_key,
            anchor_id=anchor_id,
            limit=10,
        )
        claimed = None
        for cand in candidates:
            if not cand.id:
                continue
            try:
                claimed = jobs_api.claim_job(
                    cand.id, worker_id=wid, lease_seconds=lease_seconds
                )
                break
            except ApiError as exc:
                if exc.status_code == 409:
                    continue  # another worker won this one; try the next
                raise

    if claimed is None:
        # No work: print the sentinel "None" so a polling loop can treat it as
        # "nothing to do" (exit 0).
        click.echo("None")
        return

    try:
        prepared = prepare_job(claimed)
    except JobAssetError as exc:
        raise click.ClickException(str(exc)) from exc

    # The agent file path is the only output.
    click.echo(str(prepared.agent_file))


# --------------------------------------------------------------------------
# complete-job: read the agent's output, report it
# --------------------------------------------------------------------------


@main.command("complete-job")
@click.argument("agent_file")
@click.option("--worker-id", default=None, help="Worker id (defaults to profile id).")
@click.option(
    "--confidence", type=float, default=None, help="Optional confidence 0-1."
)
@click.option("--model", default=None, help="Optional model label.")
def complete_job(
    agent_file: str, worker_id: str | None, confidence: float | None, model: str | None
) -> None:
    """Read the judgement the agent wrote and report it (complete the job).

    Takes the path `get-job` printed; the job id is the path's filename. Resolves
    the predefined output path (from the sidecar `get-job` wrote), reads the
    answer from there, and calls the complete API with it.
    """
    profile = _require_profile()
    wid = worker_id or profile.id
    job_id = job_id_from_agent_file(agent_file)
    if not job_id:
        raise click.ClickException(f"Could not derive a job id from '{agent_file}'.")

    answer_path = _resolve_answer_path(agent_file, job_id)
    answer = _read_answer(answer_path)

    with ReachifyClient(profile) as client:
        result = JobsClient(client).complete_job(
            job_id, worker_id=wid, answer=answer, confidence=confidence, model=model
        )

    click.echo(f"completed job {job_id} (status={result.status})", err=True)
    click.echo(json.dumps(answer, indent=2, default=str))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _require_profile() -> Profile:
    try:
        return load_profile()
    except ProfileError as exc:
        raise click.ClickException(str(exc)) from exc


def _resolve_answer_path(agent_file: str, job_id: str) -> Path:
    """Locate the file the agent wrote its judgement to.

    The output lives at ``<work_dir>/out/<job_id>.json`` — work_dir being the
    directory `get-job` placed the agent file in. A sidecar may record an
    explicit path; prefer whichever actually exists on disk, and otherwise
    default to the convention path (so the error message points there).
    """
    work_dir = Path(agent_file).resolve().parent
    convention = work_dir / "out" / f"{job_id}.json"

    meta = load_meta(agent_file)
    sidecar = (
        Path(str(meta["answer_path"])) if meta and meta.get("answer_path") else None
    )
    for cand in (sidecar, convention):
        if cand is not None and cand.exists():
            return cand
    return convention


def _read_answer(path: Path) -> dict[str, object]:
    """Read the agent's output file into an answer object.

    The complete API expects an object. JSON objects pass through as-is; any
    other JSON value is wrapped under ``value``; non-JSON text under ``text``.
    """
    if not path.exists():
        raise click.ClickException(f"No output found at {path}.")
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


# Wrap top-level API errors so they surface cleanly instead of as tracebacks.
def _entry() -> None:
    try:
        main()
    except ApiError as exc:
        raise SystemExit(f"API error: {exc}")


if __name__ == "__main__":
    _entry()
