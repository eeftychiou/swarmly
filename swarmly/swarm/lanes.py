"""Per-agent append-only lanes.

Each agent writes only to its own lane; everyone reads every lane. A lane holds one JSON
file per envelope, sharded by date: ``YYYY/MM/DD/<seq:08d>-<intent>.json``.

Two stores share one interface:

* :class:`DirLaneStore` keeps lanes as folders under one root (``<root>/<agent>/...``).
  The root can be a synced cloud folder (Google Drive, Dropbox, OneDrive): there is one
  writer per folder, so sync conflicts cannot happen.
* :class:`GitLaneStore` keeps each lane as an orphan branch ``refs/heads/a2a/lane/<agent>``
  in a bus clone that lives outside every agent's workspace (default
  ``~/.a2a/bus/<project>``, a bare repository). Lanes never touch ``main`` and never
  touch ``.agents/``. Writes use git plumbing and a plain fast-forward push, so nobody
  needs write access to ``main``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .envelope import GENESIS, Envelope, EnvelopeError, parse_ts

LANE_PREFIX = "refs/heads/a2a/lane/"
REMOTE_PREFIX = "refs/a2a/remote/"
#: Review P1-5: the longest any git call (a fetch or push included) may take.
GIT_TIMEOUT_SECONDS = 120.0


class LaneConflict(RuntimeError):
    """The lane moved under us (another writer, or a stale view). Rebuild and retry."""


class LaneStore:
    """Interface: ``sync()``, ``lanes()``, ``read(lane)``, ``append(env)``."""

    # (lane, problem) pairs found while syncing, e.g. a forked lane. Set by sync().
    problems: Tuple[Tuple[str, str], ...] = ()

    def sync(self) -> None:
        """Bring remote lanes up to date. A no-op for local stores."""

    def lanes(self) -> List[str]:
        raise NotImplementedError

    def read_raw(self, lane: str) -> List[str]:
        """The lane's files in seq order, as text."""
        raise NotImplementedError

    def _snapshot(self, lane: str) -> Tuple[Any, List[str]]:
        """A token for the lane's current tip, and the lane's files read at that tip."""
        return None, self.read_raw(lane)

    def _write(self, env: Envelope, relpath: str, text: str, token: Any) -> None:
        """Write ``text`` only if the lane is still at ``token``; else LaneConflict."""
        raise NotImplementedError

    @contextlib.contextmanager
    def _locked(self, lane: str) -> Iterator[None]:
        yield

    def read(self, lane: str) -> List[Envelope]:
        return [Envelope.from_json(text) for text in self.read_raw(lane)]

    def read_all(self) -> Dict[str, List[Envelope]]:
        return self.read_all_checked()[0]

    def read_all_checked(self) -> Tuple[Dict[str, List[Envelope]], List[Tuple[str, str]]]:
        """Every lane up to its first unreadable file, plus (lane, problem) for the rest.

        One bad file or one bad lane never stops a reader: the lane is cut there and the
        projection quarantines it.
        """
        lanes: Dict[str, List[Envelope]] = {}
        problems: List[Tuple[str, str]] = list(self.problems)
        for lane in self.lanes():
            envs: List[Envelope] = []
            try:
                raw = self.read_raw(lane)
            except Exception as exc:  # noqa: BLE001
                problems.append((lane, f"unreadable lane: {exc}"))
                raw = []
            for i, text in enumerate(raw):
                try:
                    envs.append(Envelope.from_json(text))
                except EnvelopeError as exc:
                    problems.append((lane, f"file {i + 1} of the lane is invalid: {exc}"))
                    break
            lanes[lane] = envs
        return lanes, problems

    def tip(self, lane: str) -> Optional[Envelope]:
        envs = self.read(lane)
        return envs[-1] if envs else None

    @staticmethod
    def relpath(env: Envelope) -> str:
        ts = parse_ts(env.ts)
        return f"{ts:%Y/%m/%d}/{env.seq:08d}-{env.intent}.json"

    def append(self, env: Envelope, *, expected_prev: Optional[str] = None) -> None:
        """Append a signed envelope; it must extend the lane's current tip exactly.

        The tip is read once and the write is a compare-and-swap against that same tip,
        so two writers drafting the same seq cannot both land. ``expected_prev`` is the
        digest of the tip the caller validated against; a lane whose stored tip differs
        (for example, an unverified envelope on top) is refused.
        """
        if not env.sig:
            raise EnvelopeError("refusing to append an unsigned envelope")
        with self._locked(env.lane):
            token, raw = self._snapshot(env.lane)
            tip = Envelope.from_json(raw[-1]) if raw else None
            want_seq = tip.seq + 1 if tip else 1
            want_prev = tip.digest() if tip else GENESIS
            if expected_prev is not None and expected_prev != want_prev:
                raise LaneConflict(f"lane {env.lane}'s stored tip is not the verified tip; "
                                   "run `swarmly verify`")
            if env.seq != want_seq or env.prev != want_prev:
                raise LaneConflict(f"lane {env.lane} is at seq {want_seq - 1}; "
                                   f"envelope has seq {env.seq}")
            self._write(env, self.relpath(env), env.to_json(), token)


class DirLaneStore(LaneStore):
    def __init__(self, root: os.PathLike) -> None:
        self.root = Path(root).expanduser()

    def lanes(self) -> List[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not
                      p.name.startswith("."))

    def _files(self, lane: str) -> List[Path]:
        base = self.root / lane
        if not base.is_dir():
            return []
        files = [p for p in base.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.json")]
        return sorted(files, key=lambda p: p.name)

    def read_raw(self, lane: str) -> List[str]:
        return [p.read_text(encoding="utf-8") for p in self._files(lane)]

    LOCK_STALE_SECONDS = 30.0

    @contextlib.contextmanager
    def _locked(self, lane: str) -> Iterator[None]:
        """One writer per lane at a time on this host: an O_EXCL lock file."""
        lock = self.root / lane / ".lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > self.LOCK_STALE_SECONDS:
                        lock.unlink()  # left by a crashed writer
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise LaneConflict(f"lane {lane} is locked by another writer") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass

    def _write(self, env: Envelope, relpath: str, text: str, token: Any) -> None:
        path = self.root / env.lane / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise LaneConflict(f"{path} already exists") from None
        finally:
            tmp.unlink(missing_ok=True)


class GitLaneStore(LaneStore):
    """Lanes as orphan branches in a (usually bare) git repository."""

    def __init__(self, repo: os.PathLike, *, remote: Optional[str] = "origin",
                 git: str = "git") -> None:
        self.repo = Path(repo).expanduser()
        self.remote = remote
        self.git_bin = git

    # -- git plumbing -----------------------------------------------------------------

    def _git(self, *args: str, input: Optional[bytes] = None, env: Optional[Dict[str, str]] = None,
             check: bool = True) -> subprocess.CompletedProcess:
        full_env = dict(os.environ)
        full_env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        if env:
            full_env.update(env)
        # Review P1-5: a hung fetch or push must not hold the poll cycle forever.
        try:
            proc = subprocess.run([self.git_bin, "-C", str(self.repo), *args], input=input,
                                  capture_output=True, env=full_env,
                                  timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"git {' '.join(args)} timed out after "
                               f"{GIT_TIMEOUT_SECONDS:.0f}s") from None
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: "
                               f"{proc.stderr.decode(errors='replace').strip()}")
        return proc

    def _out(self, *args: str) -> str:
        return self._git(*args).stdout.decode().strip()

    def _rev(self, ref: str) -> Optional[str]:
        proc = self._git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        return proc.stdout.decode().strip() or None

    def _refs(self, prefix: str) -> Dict[str, str]:
        out = self._out("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        refs = {}
        for line in out.splitlines():
            name, sha = line.split()
            refs[name[len(prefix):]] = sha
        return refs

    @classmethod
    def init(cls, path: os.PathLike, url: Optional[str] = None,
             remote: str = "origin") -> "GitLaneStore":
        """Create a bare bus clone at ``path`` that tracks only the lane refs of ``url``."""
        repo = Path(path).expanduser()
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True,
                       timeout=GIT_TIMEOUT_SECONDS)
        store = cls(repo, remote=remote if url else None)
        if url:
            if store._git("remote", "get-url", remote, check=False).returncode != 0:
                store._git("remote", "add", remote, url)
            store._git("config", f"remote.{remote}.fetch",
                       f"+{LANE_PREFIX}*:{REMOTE_PREFIX}{remote}/*")
        return store

    # -- LaneStore ----------------------------------------------------------------------

    def sync(self) -> None:
        if not self.remote:
            return
        self._git("fetch", "--quiet", "--prune", self.remote,
                  f"+{LANE_PREFIX}*:{REMOTE_PREFIX}{self.remote}/*")
        remote = self._refs(f"{REMOTE_PREFIX}{self.remote}/")
        local = self._refs(LANE_PREFIX)
        problems = []
        for lane, rsha in remote.items():
            lsha = local.get(lane)
            if lsha == rsha:
                continue
            if lsha is None or self._is_ancestor(lsha, rsha):
                self._git("update-ref", LANE_PREFIX + lane, rsha, *( [lsha] if lsha else []))
            elif not self._is_ancestor(rsha, lsha):
                # Keep the local copy and report it; never stop every reader over one lane.
                problems.append((lane, f"lane has forked: local {lsha[:12]} and remote "
                                       f"{rsha[:12]} do not share history"))
            # local is ahead: an earlier push failed; the next append pushes both.
        self.problems = tuple(problems)

    def _is_ancestor(self, a: str, b: str) -> bool:
        return self._git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    def lanes(self) -> List[str]:
        return sorted(self._refs(LANE_PREFIX))

    def read_raw(self, lane: str) -> List[str]:
        return self._read_at(self._rev(LANE_PREFIX + lane))

    def _snapshot(self, lane: str) -> Tuple[Any, List[str]]:
        sha = self._rev(LANE_PREFIX + lane)
        return sha, self._read_at(sha)

    def _read_at(self, sha: Optional[str]) -> List[str]:
        if not sha:
            return []
        names = [n for n in self._out("ls-tree", "-r", "--name-only", sha).splitlines()
                 if n.endswith(".json")]
        names.sort(key=lambda n: n.rsplit("/", 1)[-1])
        if not names:
            return []
        req = "".join(f"{sha}:{n}\n" for n in names).encode()
        raw = self._git("cat-file", "--batch", input=req).stdout
        return list(_parse_batch(raw))

    def _write(self, env: Envelope, relpath: str, text: str, token: Any) -> None:
        ref = LANE_PREFIX + env.lane
        parent: Optional[str] = token  # the commit the envelope was checked against
        blob = self._git("hash-object", "-w", "--stdin", input=text.encode("utf-8")).stdout
        blob_sha = blob.decode().strip()
        fd, index = tempfile.mkstemp(prefix="a2a-index-")
        os.close(fd)
        os.unlink(index)
        ienv = {"GIT_INDEX_FILE": index}
        try:
            if parent:
                self._git("read-tree", parent, env=ienv)
            else:
                self._git("read-tree", "--empty", env=ienv)
            self._git("update-index", "--add", "--cacheinfo", f"100644,{blob_sha},{relpath}",
                      env=ienv)
            tree = self._git("write-tree", env=ienv).stdout.decode().strip()
        finally:
            if os.path.exists(index):
                os.unlink(index)
        ident = {"GIT_AUTHOR_NAME": env.sender, "GIT_AUTHOR_EMAIL": f"{env.sender}@a2a.local",
                 "GIT_COMMITTER_NAME": env.sender,
                 "GIT_COMMITTER_EMAIL": f"{env.sender}@a2a.local",
                 "GIT_AUTHOR_DATE": env.ts, "GIT_COMMITTER_DATE": env.ts}
        args = ["commit-tree", tree, "-m", f"{env.id} {env.intent}"]
        if parent:
            args[2:2] = ["-p", parent]
        commit = self._git(*args, env=ident).stdout.decode().strip()
        # Compare-and-swap on the ref: a concurrent local writer makes this fail.
        old = parent or "0" * 40
        if self._git("update-ref", ref, commit, old, check=False).returncode != 0:
            raise LaneConflict(f"{ref} moved during append")
        if self.remote:
            push = self._git("push", "--quiet", self.remote, f"{ref}:{ref}", check=False)
            if push.returncode != 0:
                # Roll back so the next sync sees the remote's truth.
                if parent:
                    self._git("update-ref", ref, parent, commit, check=False)
                else:
                    self._git("update-ref", "-d", ref, commit, check=False)
                raise LaneConflict(f"push of {ref} was rejected: "
                                   f"{push.stderr.decode(errors='replace').strip()}")


def _parse_batch(raw: bytes) -> Iterable[str]:
    pos = 0
    while pos < len(raw):
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].split()
        if len(header) < 3 or header[1] != b"blob":
            raise EnvelopeError(f"unexpected object in lane: {raw[pos:nl]!r}")
        size = int(header[2])
        start = nl + 1
        yield raw[start:start + size].decode("utf-8")
        pos = start + size + 1


def store_from_config(bus: Dict[str, object], project: str) -> LaneStore:
    """Build a store from swarm.yaml's ``bus:`` section."""
    backend = str(bus.get("backend") or "git")
    if backend == "dir":
        path = bus.get("path")
        if not path:
            raise ValueError("bus.backend 'dir' needs bus.path")
        return DirLaneStore(Path(str(path)))
    if backend == "git":
        path = Path(str(bus.get("path") or f"~/.a2a/bus/{project}")).expanduser()
        url = bus.get("url")
        remote = str(bus.get("remote") or "origin")
        if not (path / "HEAD").exists() and not (path / ".git").exists():
            return GitLaneStore.init(path, str(url) if url else None, remote)
        return GitLaneStore(path, remote=remote if (url or bus.get("remote")) else None)
    raise ValueError(f"unknown bus.backend {backend!r} (expected git or dir)")
