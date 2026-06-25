"""Data models describing the contract between the CLI and the Reachify backend.

These are intentionally loose for the wireframe stage — tighten validation once
the real backend schema is locked down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Profile:
    """Contents of ~/.reachify/.profile — the local identity used to authenticate.

    The CLI reads this on every backend call.
    """

    #: Stable identity id; also used to namespace downloaded files on disk.
    id: str
    #: Bearer-style identity token sent to the backend.
    identity_token: str
    #: Optional override for the backend base URL (else falls back to default).
    api_base_url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        # Accept a few key spellings so the on-disk format stays forgiving.
        return cls(
            id=str(data["id"]),
            identity_token=str(
                data.get("identity_token") or data.get("identityToken") or data["token"]
            ),
            api_base_url=data.get("api_base_url") or data.get("apiBaseUrl"),
        )


@dataclass
class MaterializedFile:
    """Result of writing a single job asset to disk."""

    filename: str
    path: str
    bytes: int
    source: str  # "signed-url" | "inline-content"


# ---------------------------------------------------------------------------
# Judgement Jobs
# ---------------------------------------------------------------------------


@dataclass
class JobWorkspace:
    """Negotiated filesystem locations for a job on the worker machine."""

    work_dir: str
    out_dir: str
    answer_path: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobWorkspace":
        if not isinstance(data, dict):
            data = {}
        return cls(
            work_dir=str(data.get("work_dir") or ""),
            out_dir=str(data.get("out_dir") or ""),
            answer_path=str(data.get("answer_path") or ""),
        )


@dataclass
class JobAsset:
    """An asset served with a job (``JobAssetOut``).

    ``path`` is where the CLI must create the file (and what the prompt
    references via ``${asset:<ref>}``). Exactly one of ``content`` (inline) or
    ``bucket_path`` (+ short-lived ``signed_url``) carries the bytes.

    Every field is optional and read defensively: the backend is free to add,
    rename, or drop fields without breaking parsing. The full server payload is
    kept in :attr:`raw`.
    """

    filename: str | None = None
    ref: str | None = None
    path: str | None = None
    content: str | None = None
    bucket_path: str | None = None
    content_type: str | None = None
    signed_url: str | None = None
    signed_url_expires_in: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], ref: str | None = None) -> "JobAsset":
        return cls(
            filename=data.get("filename"),
            ref=data.get("ref") or ref,
            path=data.get("path"),
            content=data.get("content"),
            bucket_path=data.get("bucket_path"),
            content_type=data.get("content_type"),
            signed_url=data.get("signed_url"),
            signed_url_expires_in=data.get("signed_url_expires_in"),
            raw=data,
        )

    @property
    def local_name(self) -> str:
        """A usable filename even if the backend omitted ``filename``."""
        return self.filename or self.ref or "asset"


@dataclass
class Job:
    """A judgement job (``JobOut``).

    Only the fields the worker actually needs are typed, and all of them are
    read defensively so the CLI tolerates backend schema drift (added, renamed,
    or dropped fields). The full server payload is preserved in :attr:`raw`.
    """

    id: str | None
    status: str | None
    prompt: str | None
    anchor_id: str | None
    definition_key: str | None
    workspace: JobWorkspace
    assets: dict[str, JobAsset]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        if not isinstance(data, dict):
            data = {}
        assets_raw = data.get("assets") or {}
        assets: dict[str, JobAsset] = {}
        if isinstance(assets_raw, dict):
            for ref, asset in assets_raw.items():
                if isinstance(asset, dict):
                    assets[ref] = JobAsset.from_dict(asset, ref=ref)
        return cls(
            id=data.get("id"),
            status=data.get("status"),
            prompt=data.get("prompt"),
            anchor_id=data.get("anchor_id"),
            definition_key=data.get("definition_key"),
            workspace=JobWorkspace.from_dict(data.get("workspace") or {}),
            assets=assets,
            raw=data,
        )
