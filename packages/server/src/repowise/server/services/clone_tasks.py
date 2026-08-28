"""Background clone tasks for repositories added by URL.

A clone of any real repository takes longer than an HTTP request should, and
the proxy in front of a hosted deployment will cut the connection well before
git finishes. So ``POST /api/repos/remote`` starts the work and returns a
handle; the client polls until a repository id appears.

These tasks are held in memory rather than in the database on purpose. A
clone is not resumable — a server restart leaves a half-written directory
that the next attempt deletes and starts over — so a persisted row could only
ever say "this was interrupted", which is what a missing handle already says.
The registered repository itself is persisted the moment the clone lands, and
that is the part worth keeping.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from repowise.core.persistence import crud
from repowise.core.persistence.database import get_session
from repowise.server.repo_db import ensure_repo_registration, upsert_registry_row
from repowise.server.services.git_remote import (
    RemoteSource,
    RemoteSourceError,
    RepoCredentialProvider,
    clone_repository,
    is_git_checkout,
    target_path,
)

logger = logging.getLogger(__name__)

CloneStatus = Literal["pending", "running", "completed", "failed"]

# Handles are small and only useful while a client is polling. Keeping the
# most recent few hundred bounds the dict without needing a reaper task.
_MAX_TRACKED = 256


@dataclass
class CloneTask:
    """Progress of one URL-initiated repository add."""

    id: str
    slug: str
    url: str
    status: CloneStatus = "pending"
    message: str = "Queued"
    repo_id: str | None = None
    local_path: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def _touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    def advance(self, status: CloneStatus, message: str) -> None:
        self.status = status
        self.message = message
        self._touch()

    def fail(self, error: str) -> None:
        self.status = "failed"
        self.message = "Failed"
        self.error = error
        self._touch()

    def complete(self, repo_id: str, local_path: str) -> None:
        self.status = "completed"
        self.message = "Repository ready"
        self.repo_id = repo_id
        self.local_path = local_path
        self._touch()


def get_clone_tasks(app_state: Any) -> dict[str, CloneTask]:
    """The app's clone-task registry, created on first use."""
    tasks = getattr(app_state, "clone_tasks", None)
    if tasks is None:
        tasks = {}
        app_state.clone_tasks = tasks
    return tasks


def _remember(app_state: Any, task: CloneTask) -> CloneTask:
    tasks = get_clone_tasks(app_state)
    tasks[task.id] = task
    while len(tasks) > _MAX_TRACKED:
        # Insertion-ordered, so the first key is the oldest handle.
        tasks.pop(next(iter(tasks)))
    return task


def start_clone(
    app_state: Any,
    *,
    source: RemoteSource,
    name: str,
    default_branch: str | None,
    credentials: RepoCredentialProvider | None,
    settings: dict | None = None,
) -> CloneTask:
    """Kick off a clone-and-register task and return its handle immediately."""
    task = _remember(
        app_state,
        CloneTask(id=str(uuid.uuid4()), slug=source.slug, url=source.url),
    )

    async def _run() -> None:
        try:
            await _clone_and_register(
                app_state,
                task=task,
                source=source,
                name=name,
                default_branch=default_branch,
                credentials=credentials,
                settings=settings,
            )
        except RemoteSourceError as exc:
            # Expected, actionable failures: a bad URL, a private repo with no
            # token, a host that timed out. The message is already redacted.
            task.fail(str(exc))
            logger.warning("clone_task.failed", extra={"repo": source.slug, "error": str(exc)})
        except asyncio.CancelledError:
            task.fail("Clone cancelled")
            raise
        except Exception as exc:  # pragma: no cover — defensive
            task.fail(f"Unexpected error while adding {source.slug}: {exc}")
            logger.exception("clone_task.crashed", extra={"repo": source.slug})

    # Held on the task so the garbage collector cannot drop a running clone.
    task_handle = asyncio.create_task(_run())
    task._asyncio_task = task_handle
    return task


async def _clone_and_register(
    app_state: Any,
    *,
    task: CloneTask,
    source: RemoteSource,
    name: str,
    default_branch: str | None,
    credentials: RepoCredentialProvider | None,
    settings: dict | None,
) -> None:
    destination = target_path(source)

    if is_git_checkout(destination):
        # Re-adding a repository already on disk is a no-op rather than an
        # error: the operator's intent is "make this available", and the
        # checkout satisfies it. Registration below is idempotent too.
        task.advance("running", "Using the existing checkout")
        logger.info("clone_task.reusing", extra={"repo": source.slug, "path": str(destination)})
    else:
        task.advance("running", f"Cloning {source.slug}")
        await clone_repository(
            source,
            destination,
            credentials=credentials,
            branch=default_branch,
        )

    task.advance("running", "Registering repository")
    repo_id = await _register(
        app_state,
        local_path=destination,
        name=name,
        url=source.url,
        default_branch=default_branch or "main",
        settings=settings,
    )
    task.complete(repo_id, str(destination))


async def _register(
    app_state: Any,
    *,
    local_path: Path,
    name: str,
    url: str,
    default_branch: str,
    settings: dict | None,
) -> str:
    """Register the fresh checkout exactly as the local-path flow would.

    Mirrors ``create_repo`` with ``index=false``: the canonical row lands in
    the repo's own ``wiki.db`` and a registry row in the primary database
    keeps it listed across restarts. Indexing stays a separate, explicit step
    so the wizard's cost estimate still gates any spend.
    """
    repo_factory, repo_id = await ensure_repo_registration(
        app_state,
        local_path=str(local_path),
        name=name,
        url=url,
        default_branch=default_branch,
        settings=settings,
    )

    async with get_session(repo_factory) as repo_session:
        repo = await crud.get_repository(repo_session, repo_id)
        if repo is not None:
            repo.name = name
            repo.url = url
            repo.default_branch = default_branch
            if settings is not None:
                import json as _json

                repo.settings_json = _json.dumps(settings)
            await repo_session.flush()

    if repo_factory is not app_state.session_factory:
        async with get_session(app_state.session_factory) as session:
            await upsert_registry_row(
                session,
                repo_id=repo_id,
                name=name,
                local_path=str(local_path),
                url=url,
                default_branch=default_branch,
                settings=settings,
            )
            await session.commit()

    return repo_id
