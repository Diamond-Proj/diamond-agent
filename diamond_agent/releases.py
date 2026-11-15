"""Releases: code + environment + machine as one version, per channel.

A **release** ``R_n`` (per channel, n = 1, 2, …) pins a code tree
(content-addressed manifest; blobs in the core's local store), an environment
reference and the machines it targets. Creating ``R_n`` is compare-and-swap on
the channel's latest version: the caller names the version it built on and a
stale base is refused — check, lock, modify, check, release, done once, in
the core, which is the only writer.

A **build** ``R_n @ machine # k`` is that release materialised on one machine:
the code tree under the machine's workdir, verified blob by blob, plus the
machine's facts at the time (arch, host, container runtime). Snapshots are
immutable (``<workdir>/releases/<channel>/r<n>-<tree>/``), ``current`` is
swapped atomically, and a job references the snapshot path itself, so
publishing ``R_{n+1}`` never touches a running job. A machine is **aligned**
to ``R_n`` when it holds a ``ready`` build of it; the dispatch gate requires
that for a task that names a release.

The archive is packed by the caller (CLI / MCP, on the agent's side: a
directory or ``git archive <ref>``), so the core never reads local paths on
an agent's behalf. Phase 1 ships code; the environment is a reference string
exported to the job as ``DA_ENV``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .schemas import utcnow

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
CHUNK = 1_500_000            # raw bytes per shell call (base64 inflates by 4/3; the payload limit is ~10 MB)
_SUM = 'SUM=$(command -v sha256sum >/dev/null 2>&1 && echo sha256sum || echo "shasum -a 256"); '


class Conflict(ValueError):
    """The base version is not the channel's latest: someone published first."""


class Source(BaseModel):
    kind: Literal["dir", "git"]
    path: str                            # the directory, repository path or URL as the caller gave it
    ref: Optional[str] = None            # git: branch / tag / sha as given
    commit: Optional[str] = None         # git: the commit it resolved to


class FileEntry(BaseModel):
    path: str
    sha256: str
    mode: Literal["0644", "0755"]
    size: int


class Release(BaseModel):
    channel: str
    version: int
    tree: str                            # sha256 over the manifest: the identity of the code
    source: Source
    env: Optional[str] = None            # environment reference (module line, image path); the job sees it as DA_ENV
    systems: list[str]                   # the machines this release targets
    files: list[FileEntry]
    bytes: int
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def snapshot(self) -> str:
        return f"r{self.version}-{self.tree[:12]}"

    def manifest(self) -> str:
        return "".join(f"{f.mode} {f.sha256} {f.path}\n" for f in self.files)

    def summary(self) -> dict:
        return {"channel": self.channel, "version": self.version, "tree": self.tree, "snapshot": self.snapshot,
                "source": self.source.model_dump(), "env": self.env, "systems": self.systems,
                "files": len(self.files), "bytes": self.bytes, "created_at": self.created_at}


class Build(BaseModel):
    channel: str
    version: int
    system: str
    number: int                          # the k in R_n@machine#k: bumps on every (re)build
    status: Literal["building", "ready", "failed", "stale"]
    dir: Optional[str] = None            # the snapshot path on the machine (what the job runs in)
    machine: dict = Field(default_factory=dict)      # facts at build time: arch, host, container runtime
    error: Optional[str] = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None


# ------------------------------------------------------------ the core side

class BlobStore:
    """Content-addressed files on the core's disk (later: an object store)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        p = self.path(sha)
        if not p.exists():
            p.parent.mkdir(exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        return sha

    def get(self, sha: str) -> bytes:
        return self.path(sha).read_bytes()


_BAD_CHARS = set('"\\$`\n\r\t')


def _entries(archive: bytes, blobs: BlobStore) -> tuple[list[FileEntry], int]:
    """Read a tar (plain or gzip) into the blob store; the manifest comes back."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"archive is {len(archive)} bytes; the limit is {MAX_ARCHIVE_BYTES}")
    files, total, seen = [], 0, set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
        for m in tar:
            if not m.isfile():
                continue
            path = m.name[2:] if m.name.startswith("./") else m.name
            parts = path.split("/")
            if ".git" in parts:
                continue
            if (path.startswith("/") or ".." in parts or "" in parts or set(path) & _BAD_CHARS
                    or not path.isprintable()):
                raise ValueError(f"refusing path {path!r} in the archive")
            if path in seen:
                raise ValueError(f"path {path!r} appears twice in the archive")
            seen.add(path)
            data = tar.extractfile(m).read()
            files.append(FileEntry(path=path, sha256=blobs.put(data), mode="0755" if m.mode & 0o111 else "0644",
                                   size=len(data)))
            total += len(data)
    if not files:
        raise ValueError("the archive holds no files")
    files.sort(key=lambda f: f.path)
    return files, total


class ReleaseRegistry:
    """Releases and builds in the store; ``create`` is the single serialised writer."""

    def __init__(self, store, blobs: BlobStore):
        self.store = store
        self.blobs = blobs
        self._lock = threading.Lock()

    def list(self, channel_id: str) -> list[Release]:
        return sorted((Release.model_validate(d) for d in self.store.load_releases(channel_id)), key=lambda r: r.version)

    def latest_version(self, channel_id: str) -> int:
        releases = self.list(channel_id)
        return releases[-1].version if releases else 0

    def get(self, channel_id: str, version: int) -> Release:
        for r in self.list(channel_id):
            if r.version == version:
                return r
        raise KeyError(f"release {version} not found")

    def create(self, channel_id: str, archive: bytes, source: Source, env: Optional[str], systems: list[str],
               base_version: int) -> Release:
        with self._lock:
            latest = self.latest_version(channel_id)
            if base_version != latest:
                raise Conflict(f"base version {base_version} is not the channel's latest ({latest}); "
                               "read the releases again and build on the latest")
            files, total = _entries(archive, self.blobs)
            tree = hashlib.sha256("".join(f"{f.mode} {f.sha256} {f.path}\n" for f in files).encode()).hexdigest()
            release = Release(channel=channel_id, version=latest + 1, tree=tree, source=source, env=env,
                              systems=systems, files=files, bytes=total)
            self.store.save_release(channel_id, release.version, release.model_dump(mode="json"))
            return release

    # ---------------------------------------------------------------- builds

    def builds(self, channel_id: str, version: int) -> dict[str, Build]:
        return {s: Build.model_validate(d) for s, d in self.store.load_builds(channel_id, version).items()}

    def build(self, channel_id: str, version: int, system: str) -> Optional[Build]:
        return self.builds(channel_id, version).get(system)

    def save(self, build: Build) -> Build:
        self.store.save_build(build.channel, build.version, build.system, build.model_dump(mode="json"))
        return build

    def aligned(self, channel_id: str, version: int, system: str) -> Optional[Build]:
        """The ready build of this release on this machine, or None."""
        build = self.build(channel_id, version, system)
        return build if build and build.status == "ready" else None


class Aligner:
    """Materialises a release on a machine through the pool's shell and
    verifies it — what ``align`` and ``verify`` do on the remote side.

    Layout under the machine's workdir::

        releases/<channel>/.blobs/<sha256>        content-addressed cache, shared by every release
        releases/<channel>/r<n>-<tree>/           the snapshot: the tree hard-linked from the cache
        releases/<channel>/r<n>-<tree>.manifest   mode sha path, one line per file
        releases/<channel>/current -> r<n>-<tree> swapped atomically with ln -sfn
    """

    def __init__(self, pool, blobs: BlobStore):
        self.pool = pool
        self.blobs = blobs

    def _sh(self, system: str, cmd: str, what: str, timeout_s: int = 180) -> str:
        r = self.pool.shell(system, _SUM + cmd, timeout_s)
        if r.returncode != 0:
            raise RuntimeError(f"{what} failed on {system} (rc={r.returncode}): {(r.stderr or r.stdout)[-400:]}")
        return r.stdout

    @staticmethod
    def _b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    def _paths(self, release: Release, system: str) -> tuple[str, str, str]:
        base = f"{self.pool.workdir(system)}/releases/{release.channel}"
        return base, f"{base}/.blobs", f"{base}/{release.snapshot}"

    def align(self, release: Release, system: str) -> dict:
        """Make the snapshot exist and verify it; returns the machine's facts."""
        base, blobs, snap = self._paths(release, system)
        out = self._sh(system, f"mkdir -p {blobs} && echo {self._b64(release.manifest())} | base64 -d > {snap}.manifest "
                               f"&& uname -m && hostname", "preparing the release directory")
        arch, host = (out.split() + ["", ""])[:2]
        if not self._verify_ok(release, system):
            missing = self._missing(release, system)
            self._ship(release, system, missing)
            self._link(release, system)
            bad = self._verify(release, system)
            if bad:                                             # a cached blob was corrupted: ship those again
                by_path = {f.path: f.sha256 for f in release.files}
                self._ship(release, system, sorted({by_path[p] for p in bad if p in by_path}))
                self._link(release, system)
                bad = self._verify(release, system)
                if bad:
                    raise RuntimeError(f"{len(bad)} file(s) still differ after re-shipping: {', '.join(bad[:5])}")
        self._sh(system, f"cd {base} && ln -sfn {release.snapshot} current", "publishing the current pointer")
        return {"arch": arch, "host": host}

    def _missing(self, release: Release, system: str) -> list[str]:
        base, blobs, snap = self._paths(release, system)
        out = self._sh(system, f"cd {blobs} && while read mode sha path; do [ -e \"$sha\" ] || echo \"$sha\"; done "
                               f"< {snap}.manifest | sort -u", "listing cached blobs")
        return out.split()

    def _ship(self, release: Release, system: str, shas: list[str]) -> None:
        if not shas:
            return
        base, blobs, snap = self._paths(release, system)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for sha in shas:
                data = self.blobs.get(sha)
                info = tarfile.TarInfo(sha)
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
        packed = buf.getvalue()
        for i in range(0, len(packed), CHUNK):
            op = ">" if i == 0 else ">>"
            self._sh(system, f"echo {base64.b64encode(packed[i:i + CHUNK]).decode()} | base64 -d {op} {base}/incoming.tgz",
                     f"shipping blobs ({i // CHUNK + 1})")
        sums = "".join(f"{sha}  {sha}\n" for sha in shas)
        self._sh(system, f"cd {blobs} && tar xzf {base}/incoming.tgz && rm -f {base}/incoming.tgz && "
                         f"echo {self._b64(sums)} | base64 -d | $SUM -c --quiet -", "verifying shipped blobs")

    def _link(self, release: Release, system: str) -> None:
        base, blobs, snap = self._paths(release, system)
        self._sh(system, (
            f"cd {base} && rm -rf {snap}.tmp && mkdir -p {snap}.tmp && "
            f"while read mode sha path; do mkdir -p \"{snap}.tmp/$(dirname \"$path\")\"; "
            f"ln -f \".blobs/$sha\" \"{snap}.tmp/$path\" 2>/dev/null || cp \".blobs/$sha\" \"{snap}.tmp/$path\"; "
            f"if [ \"$mode\" = 0755 ]; then chmod 755 \"{snap}.tmp/$path\"; fi; done < {snap}.manifest && "
            f"printf '%s %s\\n' {release.version} {release.tree} > {snap}.tmp/.da-release && "
            f"rm -rf {snap} && mv {snap}.tmp {snap}"), "linking the snapshot")

    def _verify(self, release: Release, system: str) -> list[str]:
        """Paths that differ from the manifest (empty = the snapshot is exactly the release)."""
        base, blobs, snap = self._paths(release, system)
        r = self.pool.shell(system, _SUM + (
            f"cd {snap} 2>/dev/null || {{ echo '@@MISSING'; exit 0; }}; [ -f .da-release ] || echo '.da-release: FAILED'; "
            f"while read mode sha path; do printf '%s  %s\\n' \"$sha\" \"$path\"; done < {snap}.manifest | $SUM -c --quiet - 2>&1 | "
            f"grep -E ': FAILED' | sed 's/: FAILED.*//'"), 180)
        if "@@MISSING" in r.stdout:
            return ["<snapshot missing>"]
        return [l for l in r.stdout.splitlines() if l.strip()]

    def _verify_ok(self, release: Release, system: str) -> bool:
        return not self._verify(release, system)

    def verify(self, release: Release, system: str) -> list[str]:
        return self._verify(release, system)


# ----------------------------------------------------------- the agent side

def pack_dir(path: str) -> tuple[bytes, Source]:
    """A directory as a tar (``.git`` left out), paths relative to it."""
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError(f"{path} is not a directory")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if ".git" in rel.parts or not p.is_file():
                continue
            tar.add(p, arcname=str(rel), recursive=False)
    return buf.getvalue(), Source(kind="dir", path=str(root))


def pack_git(repo: str, ref: str = "HEAD") -> tuple[bytes, Source]:
    """``git archive`` of a commit: a local repository as is, a URL through a
    temporary blobless clone. The source records the commit it resolved to."""
    def archive(path: str) -> tuple[bytes, str]:
        commit = subprocess.run(["git", "-C", path, "rev-parse", "--verify", f"{ref}^{{commit}}"],
                                capture_output=True, text=True, check=True).stdout.strip()
        tar = subprocess.run(["git", "-C", path, "archive", "--format=tar", commit], capture_output=True, check=True).stdout
        return tar, commit
    try:
        if os.path.isdir(repo):
            tar, commit = archive(repo)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(["git", "clone", "--bare", "--quiet", "--filter=blob:none", repo, tmp], check=True,
                               capture_output=True, text=True)
                tar, commit = archive(tmp)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"git failed for {repo}@{ref}: {(exc.stderr or '').strip()[-300:]}") from None
    return tar, Source(kind="git", path=repo, ref=ref, commit=commit)
