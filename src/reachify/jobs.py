"""Judgement Jobs — worker-side capabilities.

Wraps the ``/api/v1/judgement-jobs`` endpoints the CLI uses as a worker. These
are internal building blocks; the CLI exposes only two user-facing verbs on top
of them:

    get-job       -> list + claim + materialize assets + write an agent file
    complete-job  -> get + read the predefined output + complete

Endpoints used:
    list      GET  /judgement-jobs            (poll for claimable jobs)
    get       GET  /judgement-jobs/{id}
    claim     POST /judgement-jobs/{id}/claim (atomic; 409 if lost)
    complete  POST /judgement-jobs/{id}/complete
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .api import ReachifyClient
from .config import WORKSPACE_ROOT
from .models import Job, JobAsset, MaterializedFile

_JOBS = "/api/v1/judgement-jobs"

#: ``${asset:<ref>}`` placeholders the prompt uses to reference an asset's path.
_ASSET_TOKEN = re.compile(r"\$\{asset:([^}]+)\}")


#: Extension of the rendered agent (prompt) file.
AGENT_FILE_EXT = ".md"

#: Suffix of the sidecar that records a job's resolved output path locally.
META_FILE_EXT = ".reachify.json"


def _safe_id(job_id: str) -> str:
    return (job_id or "job").replace("/", "_")


def agent_file_name(job_id: str) -> str:
    """The agent file is ``<job_id>.md``, so ``complete-job`` can recover the id
    from the path ``get-job`` printed. Slashes are neutralized for safety.
    """
    return _safe_id(job_id) + AGENT_FILE_EXT


def job_id_from_agent_file(path: str) -> str:
    """Inverse of :func:`agent_file_name`: the id is the filename without ``.md``
    (a bare id passed without the extension is returned unchanged)."""
    return Path(path).name.removesuffix(AGENT_FILE_EXT)


def meta_path_for(agent_file: str) -> Path:
    """Sidecar path that sits next to the agent file ``get-job`` printed."""
    p = Path(agent_file)
    return p.with_name(_safe_id(job_id_from_agent_file(agent_file)) + META_FILE_EXT)


def load_meta(agent_file: str) -> dict[str, Any] | None:
    """Read the sidecar `get-job` wrote, or ``None`` if it isn't there."""
    meta = meta_path_for(agent_file)
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class JobsClient:
    """Worker-facing client for the Judgement Jobs API."""

    def __init__(self, client: ReachifyClient) -> None:
        self._c = client

    def list_jobs(
        self,
        *,
        platform: str | None = None,
        anchor_type: str | None = None,
        definition_key: str | None = None,
        anchor_id: str | None = None,
        limit: int = 5,
    ) -> list[Job]:
        """Poll for up to ``limit`` claimable jobs (assets pre-signed)."""
        params = {
            "platform": platform,
            "anchor_type": anchor_type,
            "definition_key": definition_key,
            "anchor_id": anchor_id,
            "limit": limit,
        }
        params = {k: v for k, v in params.items() if v is not None}
        body = self._c.request_json("GET", _JOBS, params=params)
        # The API returns the house Page envelope ({items,total,limit,offset});
        # a worker only reads ``items``. Tolerate a bare list too (older shape).
        if isinstance(body, dict):
            items = body.get("items") or []
        else:
            items = body or []
        return [Job.from_dict(item) for item in items]

    def get_job(self, job_id: str) -> Job:
        body = self._c.request_json("GET", f"{_JOBS}/{job_id}")
        return Job.from_dict(body)

    def claim_job(
        self, job_id: str, *, worker_id: str, lease_seconds: int | None = None
    ) -> Job:
        """Atomically claim a job. Raises ApiError(status_code=409) if lost."""
        payload: dict[str, Any] = {"worker_id": worker_id}
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        body = self._c.request_json("POST", f"{_JOBS}/{job_id}/claim", json=payload)
        return Job.from_dict(body)

    def complete_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        answer: dict[str, Any],
        confidence: float | None = None,
        model: str | None = None,
        source: str | None = None,
    ) -> Job:
        """Report a successful result; records the answer onto the judgement."""
        payload: dict[str, Any] = {"worker_id": worker_id, "answer": answer}
        if confidence is not None:
            payload["confidence"] = confidence
        if model is not None:
            payload["model"] = model
        if source is not None:
            payload["source"] = source
        body = self._c.request_json("POST", f"{_JOBS}/{job_id}/complete", json=payload)
        return Job.from_dict(body)


# ---------------------------------------------------------------------------
# Workspace preparation (get-job)
# ---------------------------------------------------------------------------


class JobAssetError(Exception):
    """Raised when a job asset cannot be materialized."""


@dataclass
class PreparedJob:
    """The on-disk result of preparing a claimed job for an agent to run."""

    job: Job
    agent_file: Path  # absolute path the agent reads and executes
    answer_path: Path  # where the prompt instructs the agent to write output
    meta_file: Path  # sidecar recording answer_path for complete-job
    assets: list[MaterializedFile]


def work_dir_for(job: Job) -> Path:
    """Base directory for a job's files (backend-specified, with a fallback)."""
    if job.workspace.work_dir:
        return Path(job.workspace.work_dir)
    return WORKSPACE_ROOT / (job.id or "job")


def answer_path_for(job: Job) -> Path:
    """Where the agent writes its judgement.

    Prefer the backend's negotiated output location (``job.output.path``);
    otherwise fall back to the convention ``<out_dir or work_dir/out>/<job_id>.json``.
    """
    if job.output and job.output.path:
        return Path(job.output.path)
    ws = job.workspace
    out_dir = Path(ws.out_dir) if ws.out_dir else work_dir_for(job) / "out"
    return out_dir / f"{_safe_id(job.id or '')}.json"


def asset_target(asset: JobAsset, work_dir: Path) -> Path:
    """Absolute path where an asset is written (and what the prompt references)."""
    return Path(asset.path) if asset.path else work_dir / asset.local_name


def prepare_job(job: Job) -> PreparedJob:
    """Materialize a claimed job's assets and write its agent file.

    Returns the absolute path of the agent file (the rendered prompt, with every
    ``${asset:<ref>}`` resolved to the asset's on-disk path) plus the predefined
    output path the prompt tells the agent to write to.
    """
    work_dir = work_dir_for(job)
    asset_paths = {
        ref: asset_target(asset, work_dir) for ref, asset in job.assets.items()
    }

    written = [_materialize_one(asset, asset_paths[ref]) for ref, asset in job.assets.items()]

    agent_file = work_dir / agent_file_name(job.id or "")
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    agent_file.write_text(job.prompt or "", encoding="utf-8")

    answer_path = answer_path_for(job)

    # Persist the resolved output path so complete-job can find it locally —
    # the plain GET /judgement-jobs/{id} does not echo the workspace back.
    meta_file = meta_path_for(str(agent_file))
    meta_file.write_text(
        json.dumps({"job_id": job.id, "answer_path": str(answer_path)}, indent=2),
        encoding="utf-8",
    )

    return PreparedJob(
        job=job,
        agent_file=agent_file,
        answer_path=answer_path,
        meta_file=meta_file,
        assets=written,
    )

def _materialize_one(asset: JobAsset, target: Path) -> MaterializedFile:
    target.parent.mkdir(parents=True, exist_ok=True)
    if asset.signed_url:
        nbytes = _download(asset.signed_url, target)
        source = "signed-url"
    elif asset.content is not None:
        nbytes = target.write_bytes(asset.content.encode("utf-8"))
        source = "inline-content"
    else:
        raise JobAssetError(
            f"Asset '{asset.ref or asset.local_name}' has neither signed_url nor content."
        )
    return MaterializedFile(
        filename=asset.local_name, path=str(target), bytes=nbytes, source=source
    )


def _download(url: str, dest: Path) -> int:
    written = 0
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                    written += len(chunk)
    except httpx.HTTPError as exc:
        raise JobAssetError(f"Failed downloading {url}: {exc}") from exc
    return written
