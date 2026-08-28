"""Tests for adding repositories by URL (/api/repos/remote).

The clone itself is stubbed everywhere except where the subject *is* the
clone command: these tests are about URL handling, the task lifecycle and
what leaves the process, none of which should need the network.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

from repowise.server.services import git_remote
from repowise.server.services.git_remote import (
    RemoteSourceError,
    StaticTokenProvider,
    parse_remote_url,
    target_path,
)

# --------------------------------------------------------------------------
# URL normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_url", "expected_dir"),
    [
        (
            "https://github.com/acme/app",
            "https://github.com/acme/app",
            "github.com/acme/app",
        ),
        # .git suffix is stripped from both the URL and the checkout name.
        (
            "https://github.com/acme/app.git",
            "https://github.com/acme/app",
            "github.com/acme/app",
        ),
        # scp-style ssh syntax is rewritten to https.
        (
            "git@github.com:acme/app.git",
            "https://github.com/acme/app",
            "github.com/acme/app",
        ),
        # Bare shorthand resolves against the default host.
        ("acme/app", "https://github.com/acme/app", "github.com/acme/app"),
        # A missing scheme is assumed rather than rejected.
        ("github.com/acme/app", "https://github.com/acme/app", "github.com/acme/app"),
        # Nested GitLab namespaces survive into the checkout path, so
        # group-a/team/app and group-b/team/app cannot collide.
        (
            "https://gitlab.com/group/team/app",
            "https://gitlab.com/group/team/app",
            "gitlab.com/group/team/app",
        ),
    ],
)
def test_parse_remote_url_normalises(raw: str, expected_url: str, expected_dir: str) -> None:
    source = parse_remote_url(raw)
    assert source.url == expected_url
    assert source.relative_dir().as_posix() == expected_dir


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        # A repo needs an owner as well as a name.
        "https://github.com/onlyone",
        # Only https is cloned: ssh needs key material the server does not
        # manage, and file:// would make this an arbitrary local-path read.
        "ssh://git@github.com/acme/app",
        "file:///etc/passwd",
        "ftp://example.com/acme/app",
        # Credentials belong in the token field, not the URL, or the clone
        # writes them into .git/config.
        "https://tok@github.com/acme/app",
        "https://user:pass@github.com/acme/app",
    ],
)
def test_parse_remote_url_rejects(raw: str) -> None:
    with pytest.raises(RemoteSourceError):
        parse_remote_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        # "." and ".." match the safe-character class (dots are legal in repo
        # names) but as path segments they walk out of the managed root.
        "https://github.com/acme/../../../etc",
        "https://github.com/../evil",
        "https://github.com/acme/./app",
        "https://github.com/%2e%2e/evil",
    ],
)
def test_parse_remote_url_rejects_traversal(raw: str) -> None:
    with pytest.raises(RemoteSourceError):
        parse_remote_url(raw)


def test_target_path_stays_under_the_managed_root(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    resolved = target_path(parse_remote_url("https://gitlab.com/group/team/app"), root=root)
    assert resolved.is_relative_to(root)
    assert resolved == root / "gitlab.com" / "group" / "team" / "app"


def test_clone_root_prefers_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOWISE_REPOS_DIR", "/srv/checkouts")
    assert git_remote.clone_root() == Path("/srv/checkouts")

    # Otherwise clones sit beside the rest of the server's state, which on the
    # container image is the mounted volume.
    monkeypatch.delenv("REPOWISE_REPOS_DIR")
    monkeypatch.setenv("REPOWISE_DATA_DIR", "/data")
    assert git_remote.clone_root() == Path("/data/repos")


# --------------------------------------------------------------------------
# Clone mechanics
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "already-here.txt").write_text("hi")

    with pytest.raises(RemoteSourceError, match="not empty"):
        await git_remote.clone_repository(parse_remote_url("acme/app"), destination)


@pytest.mark.asyncio
async def test_clone_never_puts_the_token_on_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token in argv is readable by every process on the host, and one in
    the URL is persisted into .git/config by the clone. It must travel only
    through the askpass helper's environment."""
    secret = "ghp_secret_value_do_not_leak"
    captured: dict[str, object] = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env", {})

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await git_remote.clone_repository(
        parse_remote_url("acme/app"),
        tmp_path / "dest",
        credentials=StaticTokenProvider(secret),
    )

    argv = captured["args"]
    assert all(secret not in str(a) for a in argv), f"token leaked into argv: {argv}"
    assert "https://github.com/acme/app" in argv

    env = captured["env"]
    # It reaches git only through the askpass helper.
    assert env["REPOWISE_GIT_TOKEN"] == secret
    assert env["GIT_ASKPASS"].endswith("askpass.sh")
    # And git must never block on an interactive prompt in a server process.
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.asyncio
async def test_clone_failure_redacts_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git echoes what it was given; the error reaches an API response and
    the logs, so anything secret is scrubbed on the way out."""
    secret = "ghp_secret_value_do_not_leak"

    async def fake_exec(*args, **kwargs):
        class _Proc:
            returncode = 128

            async def communicate(self):
                return (f"fatal: could not read from https://{secret}@github.com".encode(), b"")

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RemoteSourceError) as excinfo:
        await git_remote.clone_repository(
            parse_remote_url("acme/app"),
            tmp_path / "dest",
            credentials=StaticTokenProvider(secret),
        )
    assert secret not in str(excinfo.value)
    assert "***" in str(excinfo.value)


@pytest.mark.asyncio
async def test_clone_hints_at_a_token_when_unauthenticated_access_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_exec(*args, **kwargs):
        class _Proc:
            returncode = 128

            async def communicate(self):
                return (b"fatal: Authentication failed", b"")

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RemoteSourceError, match="private"):
        await git_remote.clone_repository(parse_remote_url("acme/app"), tmp_path / "dest")


@pytest.mark.asyncio
async def test_clone_removes_a_partial_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written directory would block the retry with "not empty"."""
    destination = tmp_path / "dest"

    async def fake_exec(*args, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partial").write_text("half a clone")

        class _Proc:
            returncode = 1

            async def communicate(self):
                return (b"fatal: early EOF", b"")

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RemoteSourceError):
        await git_remote.clone_repository(parse_remote_url("acme/app"), destination)
    assert not destination.exists()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_add_rejects_a_bad_url(client: AsyncClient) -> None:
    resp = await client.post("/api/repos/remote", json={"url": "file:///etc/passwd"})
    assert resp.status_code == 422
    assert "https" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_remote_add_returns_a_handle_then_registers(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The POST returns 202 immediately; the repository appears once the
    clone lands and the handle carries its id."""
    monkeypatch.setenv("REPOWISE_REPOS_DIR", str(tmp_path / "checkouts"))

    async def fake_clone(source, destination, **kwargs):
        # Stand in for git: a real checkout, minus the network.
        destination.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--quiet", str(destination)], check=True)

    monkeypatch.setattr(git_remote, "clone_repository", fake_clone)
    monkeypatch.setattr("repowise.server.services.clone_tasks.clone_repository", fake_clone)

    resp = await client.post(
        "/api/repos/remote",
        json={"url": "https://github.com/acme/app", "access_token": "ghp_secret"},
    )
    assert resp.status_code == 202
    handle = resp.json()
    assert handle["slug"] == "acme/app"
    assert handle["repo_id"] is None
    # The token must not come back in the response that echoes the request.
    assert "ghp_secret" not in resp.text

    clone_id = handle["clone_id"]
    for _ in range(100):
        poll = await client.get(f"/api/repos/remote/{clone_id}")
        assert poll.status_code == 200
        body = poll.json()
        if body["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)

    assert body["status"] == "completed", body.get("error")
    assert body["repo_id"]
    assert body["local_path"].endswith("github.com/acme/app")

    listed = await client.get("/api/repos")
    assert any(r["id"] == body["repo_id"] for r in listed.json())


@pytest.mark.asyncio
async def test_remote_add_surfaces_a_clone_failure(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOWISE_REPOS_DIR", str(tmp_path / "checkouts"))

    async def failing_clone(source, destination, **kwargs):
        raise RemoteSourceError("git clone failed for acme/app: repository not found")

    monkeypatch.setattr("repowise.server.services.clone_tasks.clone_repository", failing_clone)

    resp = await client.post("/api/repos/remote", json={"url": "https://github.com/acme/app"})
    clone_id = resp.json()["clone_id"]

    for _ in range(100):
        body = (await client.get(f"/api/repos/remote/{clone_id}")).json()
        if body["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)

    assert body["status"] == "failed"
    assert "repository not found" in body["error"]
    assert body["repo_id"] is None


@pytest.mark.asyncio
async def test_unknown_clone_handle_is_404(client: AsyncClient) -> None:
    resp = await client.get("/api/repos/remote/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remote_route_is_not_shadowed_by_the_repo_id_route(
    client: AsyncClient,
) -> None:
    """ "/remote/{id}" is declared before "/{repo_id}/..."; if that ordering
    regresses this returns a repo-shaped 404 from the wrong handler."""
    resp = await client.get("/api/repos/remote/abc123")
    assert resp.json()["detail"] == "Clone task not found"
