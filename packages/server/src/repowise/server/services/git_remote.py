"""Cloning repositories from remote git hosts.

The local-path registration flow assumes an operator who can put a checkout
on the same filesystem as the server. That assumption breaks the moment
repowise runs anywhere but the developer's laptop: on a container host there
is no path to type. This module is the other half — given a remote URL, put a
real checkout on the server's disk so the existing pipeline has something to
read.

Three things are deliberate here:

* **Credentials never touch argv or ``.git/config``.** A token embedded in
  the clone URL is persisted by git into the remote config and shows up in
  ``ps`` output for every process on the box. Instead the token is handed to
  git through ``GIT_ASKPASS``, so it lives only in the child's environment
  and a private temp file that is deleted before this function returns.

* **Clones are full.** repowise derives ownership and churn from ``git log``;
  a ``--depth 1`` clone silently degrades that analysis rather than failing
  loudly, which is the worst kind of wrong.

* **Where a token comes from is behind an interface.** Today it is one the
  operator typed. A GitHub App minting short-lived installation tokens is the
  reason ``RepoCredentialProvider`` exists rather than a plain ``str``
  parameter threaded through.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# Clones write to the server's disk, so a host/owner/name that escapes the
# managed root would let a caller choose where. Every segment used to build a
# path is matched against this first.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# scp-style syntax git accepts but urlsplit does not: git@github.com:owner/repo.git
_SCP_SYNTAX = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>.+)$")

# Bare "owner/repo" shorthand, resolved against the default host.
_SHORTHAND = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

DEFAULT_HOST = "github.com"

# https is the only scheme cloned. ssh needs key material this server has no
# business managing, and file:// would turn "add a repository" into an
# arbitrary-local-path read for anyone who can reach the API.
ALLOWED_SCHEMES = ("https",)

DEFAULT_CLONE_TIMEOUT_SECONDS = 1800


class RemoteSourceError(ValueError):
    """A remote URL that cannot be turned into something safe to clone."""


@dataclass(frozen=True)
class RemoteSource:
    """A parsed, normalised remote ready to be cloned."""

    url: str
    """Canonical https clone URL, with any embedded credentials stripped."""

    host: str
    owner: str
    name: str
    path_segments: tuple[str, ...] = ()
    """Every path segment, so nested namespaces survive into the checkout."""

    @property
    def slug(self) -> str:
        return "/".join(self.path_segments) if self.path_segments else f"{self.owner}/{self.name}"

    def relative_dir(self) -> Path:
        """Path of this repo beneath the managed clone root.

        Namespaced by host and by the remote's *full* path, so
        ``github.com/acme/app`` and ``gitlab.com/acme/app`` are different
        checkouts — and so are GitLab's ``group-a/team/app`` and
        ``group-b/team/app``, which would collide if only the last two
        segments were kept.
        """
        return Path(self.host).joinpath(*self.path_segments)


def _require_safe(segment: str, label: str) -> str:
    # "." and ".." match the character class below (dots are legal in repo
    # names), but as path segments they walk out of the managed root and as
    # URL segments they are not a repository. Rejected before anything else.
    if segment in (".", ".."):
        raise RemoteSourceError(f"{label} must not be {segment!r}")
    if not _SAFE_SEGMENT.match(segment):
        raise RemoteSourceError(f"unsupported characters in {label}: {segment!r}")
    return segment


def parse_remote_url(raw: str, *, default_host: str = DEFAULT_HOST) -> RemoteSource:
    """Normalise a user-supplied remote into a :class:`RemoteSource`.

    Accepts the three forms people actually paste: a full https URL, GitHub's
    ``git@host:owner/repo.git`` ssh syntax, and bare ``owner/repo`` shorthand.
    The latter two are rewritten to https, which is the only scheme cloned.

    Credentials embedded in the URL (``https://token@host/...``) are rejected
    rather than quietly used: they would be written into ``.git/config`` by
    the clone, and the caller has a dedicated field for a token.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise RemoteSourceError("a repository URL is required")

    if _SHORTHAND.match(candidate):
        candidate = f"https://{default_host}/{candidate}"
    elif (scp := _SCP_SYNTAX.match(candidate)) is not None:
        candidate = f"https://{scp.group('host')}/{scp.group('path')}"
    elif "://" not in candidate:
        # A bare host/owner/repo with no scheme — assume https rather than
        # rejecting something whose intent is unambiguous.
        candidate = f"https://{candidate}"

    parts = urlsplit(candidate)

    if parts.scheme not in ALLOWED_SCHEMES:
        raise RemoteSourceError(
            f"unsupported scheme {parts.scheme!r}; only "
            f"{', '.join(ALLOWED_SCHEMES)} remotes can be cloned"
        )
    if parts.username or parts.password:
        raise RemoteSourceError(
            "remove the credentials from the URL and supply an access token instead"
        )
    if not parts.hostname:
        raise RemoteSourceError(f"could not read a host from {raw!r}")

    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        raise RemoteSourceError(
            f"expected a URL of the form https://host/owner/repository, got {raw!r}"
        )

    # Deeper paths are normal on self-hosted GitLab (groups/subgroups). Keep
    # the trailing element as the repo and the one before it as the owner,
    # which is what both GitHub and GitLab mean by those positions.
    name = segments[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    segments[-1] = name

    host = _require_safe(parts.hostname, "host")
    # Every segment is validated, not just the last two: an unchecked middle
    # segment still reaches the filesystem through relative_dir().
    checked = tuple(_require_safe(s, "path segment") for s in segments)

    # Rebuild from validated parts: whatever query, fragment or userinfo came
    # in is dropped rather than carried into the clone.
    url = urlunsplit((parts.scheme, host, "/".join(checked), "", ""))
    return RemoteSource(
        url=url,
        host=host,
        owner=checked[-2],
        name=checked[-1],
        path_segments=checked,
    )


class RepoCredentialProvider(Protocol):
    """Resolves the token to authenticate a clone of ``source``, if any.

    Implemented today by :class:`StaticTokenProvider` (an operator-supplied
    PAT). A GitHub App provider minting short-lived installation tokens per
    clone satisfies the same protocol and needs no change above this line.
    """

    async def token_for(self, source: RemoteSource) -> str | None: ...

    @property
    def username(self) -> str:
        """Username paired with the token in basic auth."""
        ...


class NullCredentialProvider:
    """Anonymous access — public repositories only."""

    username = "git"

    async def token_for(self, source: RemoteSource) -> str | None:
        return None


class StaticTokenProvider:
    """A single operator-supplied token, used for every host.

    ``x-access-token`` is what GitHub expects as the username for both PATs
    and App installation tokens; GitLab and Bitbucket ignore the username
    when the password is a token, so one default works across hosts.
    """

    def __init__(self, token: str, *, username: str = "x-access-token") -> None:
        self._token = token
        self.username = username

    async def token_for(self, source: RemoteSource) -> str | None:
        return self._token or None


def clone_root() -> Path:
    """Directory that holds server-managed clones.

    ``REPOWISE_REPOS_DIR`` wins when set. Otherwise clones land next to the
    rest of the server's state, which on the container image is the mounted
    volume — so a redeploy does not throw away everything that was indexed.
    """
    configured = os.environ.get("REPOWISE_REPOS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    data_dir = os.environ.get("REPOWISE_DATA_DIR")
    if data_dir:
        return (Path(data_dir).expanduser() / "repos").resolve()
    return (Path.home() / ".repowise" / "repos").resolve()


def target_path(source: RemoteSource, *, root: Path | None = None) -> Path:
    """Absolute checkout path for ``source``, guaranteed to sit under the root."""
    base = (root or clone_root()).resolve()
    resolved = (base / source.relative_dir()).resolve()
    # Defence in depth: every segment is already validated, so this can only
    # fire if that validation is later loosened.
    if not resolved.is_relative_to(base):
        raise RemoteSourceError("refusing to clone outside the managed repository root")
    return resolved


def is_git_checkout(path: Path) -> bool:
    return (path / ".git").exists()


class _AskpassScript:
    """A private, self-deleting ``GIT_ASKPASS`` helper.

    git invokes this once for the username and once for the password. The
    token reaches it through the environment rather than the command line, so
    it never appears in ``ps`` output or in the cloned ``.git/config``.
    """

    def __init__(self, username: str, token: str) -> None:
        self._username = username
        self._token = token
        self._dir: str | None = None
        self.path: Path | None = None

    def __enter__(self) -> _AskpassScript:
        self._dir = tempfile.mkdtemp(prefix="repowise-askpass-")
        os.chmod(self._dir, stat.S_IRWXU)
        script = Path(self._dir) / "askpass.sh"
        script.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *[Uu]sername*) printf %s "$REPOWISE_GIT_USERNAME" ;;\n'
            '  *) printf %s "$REPOWISE_GIT_TOKEN" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        script.chmod(stat.S_IRWXU)
        self.path = script
        return self

    def __exit__(self, *exc: object) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = None
        self.path = None

    def env(self) -> dict[str, str]:
        return {
            "GIT_ASKPASS": str(self.path),
            "REPOWISE_GIT_USERNAME": self._username,
            "REPOWISE_GIT_TOKEN": self._token,
        }


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Strip anything secret out of git's own output before it is surfaced.

    git echoes the remote URL in its errors, and a misconfigured helper can
    echo more than that. Error text reaches an API response and the logs, so
    it is redacted on the way out.
    """
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


async def clone_repository(
    source: RemoteSource,
    destination: Path,
    *,
    credentials: RepoCredentialProvider | None = None,
    branch: str | None = None,
    timeout: float = DEFAULT_CLONE_TIMEOUT_SECONDS,
) -> None:
    """Clone ``source`` into ``destination`` with full history.

    Raises :class:`RemoteSourceError` with git's own (redacted) message when
    the clone fails, so the caller can show the operator something actionable
    — a bad URL and a missing token look nothing alike and both are common.
    """
    if destination.exists() and any(destination.iterdir()):
        raise RemoteSourceError(f"destination is not empty: {destination}")

    provider = credentials or NullCredentialProvider()
    token = await provider.token_for(source)

    args = [
        "git",
        # Never consult the system/user credential helpers: on a shared host
        # they could supply someone else's stored credentials.
        "-c",
        "credential.helper=",
        "clone",
        "--quiet",
    ]
    if branch:
        args += ["--branch", branch]
    args += ["--", source.url, str(destination)]

    env = {
        **os.environ,
        # Without this a private repo with no token hangs forever waiting for
        # a terminal that does not exist, instead of failing.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    destination.parent.mkdir(parents=True, exist_ok=True)

    with _AskpassScript(provider.username, token or "") as askpass:
        if token:
            env.update(askpass.env())

        logger.info(
            "clone.start",
            extra={"repo": source.slug, "host": source.host, "authenticated": bool(token)},
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            _cleanup_partial(destination)
            raise RemoteSourceError(
                f"clone of {source.slug} timed out after {int(timeout)}s"
            ) from None
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            _cleanup_partial(destination)
            raise

    if proc.returncode != 0:
        detail = _scrub((output or b"").decode("utf-8", "replace").strip(), (token or "",))
        _cleanup_partial(destination)
        hint = ""
        if not token and _looks_like_auth_failure(detail):
            hint = " — if this repository is private, supply an access token"
        raise RemoteSourceError(
            f"git clone failed for {source.slug}{hint}: {detail or 'no output from git'}"
        )

    logger.info("clone.done", extra={"repo": source.slug, "path": str(destination)})


def _looks_like_auth_failure(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in ("authentication", "not found", "permission denied", "could not read")
    )


def _cleanup_partial(destination: Path) -> None:
    """Remove a half-written checkout so a retry is not blocked by it."""
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
