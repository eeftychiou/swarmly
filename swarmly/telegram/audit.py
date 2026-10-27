"""Pre-publication audit: does a public repository still carry a Telegram identifier? (#1).

The redactor (:mod:`swarmly.telegram.redact`) stops the raw values leaving a host, but nothing
could ask the other question -- *what is already out there?* -- and a publication incident
(the maintainer's handle, a chat id and bot usernames in bus envelopes and in issue/PR text,
which then had to be purged) is exactly the case where reading the repository is the only way
to know. This module is that read: it names the strings that must not be public, and looks for
each of them everywhere a public repository keeps text:

- **every ref**, through a mirror clone (:func:`mirror_clone`) -- ``refs/pull/*`` included,
  because a pull request's head is as public as ``main`` and a mirror clone is what fetches
  it; the files (blobs) reachable from those refs and their commit messages;
- **the issue and pull-request side** -- bodies, comments and review comments, through ``gh``.

Nothing here writes: it reports, and the caller exits non-zero on any hit so the audit can be
a gate before publication. An excerpt in a report is passed through the redactor first, so
running the audit never copies the value it just found into a terminal, a log or a PR.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from swarmly.telegram.redact import CHAT, PATH, USER, Redactor

logger = logging.getLogger("swarmly.telegram.audit")

__all__ = ["Needle", "Hit", "Report", "needles_from", "scan_text", "mirror_clone", "scan_refs",
           "scan_commits", "scan_github", "audit", "EXIT_CLEAN", "EXIT_HITS", "EXIT_BROKEN"]

#: Nothing sensitive found: the repository may be published.
EXIT_CLEAN = 0
#: At least one configured value is public. The caller must not publish until it is purged.
EXIT_HITS = 1
#: The audit could not be run (no git, no clone, no ``gh``): never "clean".
EXIT_BROKEN = 2

#: How much of a line around a hit an excerpt keeps. Enough to find it by hand, little enough
#: that a report is not a copy of the file.
EXCERPT = 60

#: How many blobs one ``git cat-file --batch`` call reads. A chunk keeps a big repository's
#: whole history out of memory without paying a process per blob.
CHUNK = 200

#: The backstops :class:`~swarmly.telegram.redact.Redactor` scans for even when no config
#: names them: a Telegram chat id, and an absolute path under a home directory.
_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (CHAT, re.compile(r"(?<!\d)-100\d{6,}(?!\d)")),
    (PATH, re.compile(r"(?i)\b[A-Z]:\\{1,2}(?:Users|Documents and Settings)\\{1,2}[^\\\s\"']+")),
    (PATH, re.compile(r"(?<![\w/])/(?:home|Users)/[^/\s\"']+")),
)


@dataclass(frozen=True)
class Needle:
    """One string that must not be public, and the placeholder that names what it is."""

    label: str          # "<chat>", "<maintainer>", "<path>", "<user>"
    value: str          # the literal to look for

    def __str__(self) -> str:                       # in a report, never the value itself
        return self.label


@dataclass(frozen=True)
class Hit:
    """One place a needle was found. *excerpt* is already redacted."""

    where: str          # `refs/heads/main: docs/x.md`, `commit abc1234`, `issue #12 body`
    needle: str         # the label, never the value
    excerpt: str

    def __str__(self) -> str:
        return f"{self.needle} in {self.where}: {self.excerpt}"


@dataclass
class Report:
    """What one run of :func:`audit` found, and how much it looked at."""

    repo: str = ""
    refs: List[str] = field(default_factory=list)
    commits: int = 0
    blobs: int = 0
    items: int = 0                                    # issue/PR bodies, comments, reviews
    hits: List[Hit] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)  # what could not be scanned

    @property
    def status(self) -> int:
        """The exit status: a hit is 1, anything that could not be scanned is 2."""
        if self.problems:
            return EXIT_BROKEN
        return EXIT_HITS if self.hits else EXIT_CLEAN

    def summary(self) -> str:
        """One line: what was scanned and what was found."""
        said = (f"{self.repo or 'the repository'}: {len(self.refs)} ref(s), {self.commits} "
                f"commit(s), {self.blobs} file(s), {self.items} issue/PR item(s) -- ")
        if self.hits:
            said += f"{len(self.hits)} hit(s)"
        else:
            said += "no hit"
        if self.problems:
            said += "; not scanned: " + "; ".join(self.problems)
        return said


# ------------------------------------------------------------------------------------
# What must not be public
# ------------------------------------------------------------------------------------


def needles_from(config: Any) -> List[Needle]:
    """The strings a config says must not be public, plus the two backstops (#1).

    The same set the redactor replaces on the way out -- the maintainer's handle,
    ``public_redactions``, chat ids, long user ids, absolute local paths -- so what the audit
    looks for and what the transport would scrub cannot drift apart.
    """
    redactor = Redactor.from_config(config)
    found: Dict[Tuple[str, str], Needle] = {}

    def add(label: str, value: Any) -> None:
        text = str(value or "")
        if len(text) < 4:                      # a short value is everywhere and means nothing
            return
        found.setdefault((label, text), Needle(label, text))

    for literal, replacement in (redactor.literals or {}).items():
        add(str(replacement or literal), literal)
    for chat in redactor.chat_ids:
        add(CHAT, chat)
        add(CHAT, str(abs(int(chat))))
    for user in redactor.user_ids:
        if len(str(abs(int(user)))) >= 5:
            add(USER, user)
            add(USER, str(abs(int(user))))
    for path in redactor.paths:
        add(PATH, path)
        add(PATH, str(path).replace("\\", "/"))
    return list(found.values())


def scan_text(text: str, needles: Sequence[Needle],
              redactor: Optional[Redactor] = None) -> List[Tuple[str, str]]:
    """``[(label, excerpt)]`` for every needle in *text*: one entry per distinct needle.

    *redactor*, when given, scrubs the excerpt before it is returned -- a report about a leak
    must not be one.
    """
    body = str(text or "")
    if not body:
        return []
    found: List[Tuple[str, str]] = []
    seen = set()
    for needle in needles:
        at = body.find(needle.value)
        if at < 0 or needle.label in seen:
            continue
        seen.add(needle.label)
        found.append((needle.label, _excerpt(body, at, len(needle.value), redactor, needles)))
    for label, pattern in _PATTERNS:
        match = pattern.search(body)
        if match is None or label in seen:
            continue
        # A backstop says "a chat id", not "this chat id": one line per kind is enough, and the
        # exact value is what the report must not carry.
        seen.add(label)
        found.append((label, _excerpt(body, match.start(), len(match.group(0)), redactor,
                                      needles)))
    return found


def _excerpt(body: str, at: int, size: int, redactor: Optional[Redactor],
             needles: Sequence[Needle] = ()) -> str:
    """The line around a hit, trimmed and -- first thing -- redacted.

    Without a redactor the needles themselves are replaced by their labels, so an excerpt never
    carries the raw value however the caller built the scan: a report about a leak is the one
    thing that must not become one.
    """
    start = max(0, at - EXCERPT)
    end = min(len(body), at + size + EXCERPT)
    said = " ".join(body[start:end].split())
    if redactor is not None:
        return redactor.text(said)
    for needle in needles:
        if needle.value:
            said = said.replace(needle.value, needle.label)
    return said


# ------------------------------------------------------------------------------------
# The git side: every ref of a mirror clone
# ------------------------------------------------------------------------------------


class AuditError(RuntimeError):
    """The audit could not look -- never read as "nothing found"."""


def _git(root: Path, *args: str, data: Optional[bytes] = None,
         text: bool = True) -> Any:
    """One ``git`` call in *root*. Raises :class:`AuditError` when git could not answer."""
    command = ["git", "-C", str(root), *args]
    try:
        done = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError(f"git {args[0]} could not run: {exc}") from exc
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise AuditError(f"git {args[0]} failed: {said}")
    return done.stdout.decode("utf-8", "replace") if text else done.stdout


def mirror_clone(url: str, folder: Optional[Path] = None, *, fetch: bool = True) -> Path:
    """A mirror clone of *url* -- every ref, ``refs/pull/*`` included (#1) -- and its path.

    *folder* is where it lives: an existing clone there is fetched and reused (a large
    repository is not downloaded again on every audit), anything else is cloned into it. With
    no *folder*, one is made under the system's temporary directory for the caller to remove.
    """
    target = Path(folder) if folder is not None else Path(tempfile.mkdtemp(prefix="audit-"))
    if (target / "HEAD").exists() or (target / "objects").is_dir():
        # An existing clone is fetched, never re-cloned: a large repository is downloaded once.
        # One with no ``origin`` (a mirror made by hand, or a local path) is used as it stands.
        if fetch and "origin" in _git(target, "remote").split():
            _git(target, "fetch", "--prune", "--quiet", "origin")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(["git", "clone", "--mirror", "--quiet", str(url), str(target)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError(f"could not clone {url}: {exc}") from exc
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise AuditError(f"could not clone {url}: {said}")
    return target


def refs_of(root: Path) -> List[str]:
    """Every ref in the clone. ``refs/pull/*`` shows up here when the mirror fetched it."""
    return [line.strip() for line in _git(root, "for-each-ref", "--format=%(refname)"
                                          ).splitlines() if line.strip()]


def scan_refs(root: Path, needles: Sequence[Needle],
              redactor: Optional[Redactor] = None) -> Tuple[int, int, List[Hit]]:
    """``(blobs, files, hits)`` over the files reachable from every ref.

    The reachable set is ``rev-list --objects --all`` : every ref, so a file that exists only
    on a pull request's head or an abandoned branch is scanned too -- that is where a leak
    outlives the fix on ``main``.
    """
    listing = _git(root, "rev-list", "--objects", "--all").splitlines()
    paths: Dict[str, str] = {}
    for line in listing:
        sha, _, path = line.partition(" ")
        if path and sha and sha not in paths:
            paths[sha] = path                    # the first path a blob is reached by
    hits: List[Hit] = []
    blobs = 0
    for chunk in _chunks(list(paths), CHUNK):
        for sha, kind, payload in _objects(root, chunk):
            if kind != "blob":
                continue
            blobs += 1
            for label, excerpt in scan_text(payload.decode("utf-8", "replace"), needles,
                                            redactor):
                hits.append(Hit(where=paths.get(sha, sha), needle=label, excerpt=excerpt))
    return blobs, len(paths), hits


def scan_commits(root: Path, needles: Sequence[Needle],
                 redactor: Optional[Redactor] = None) -> Tuple[int, List[Hit]]:
    """``(commits, hits)``: the message of every commit any ref reaches, and of every tag.

    A commit message is public for as long as the commit is reachable, so it is as much a
    place for an identifier as a file is.
    """
    out = _git(root, "log", "--all", "--format=%H%x00%B%x00%x00", "--no-color")
    hits: List[Hit] = []
    count = 0
    for record in out.split("\x00\x00"):
        record = record.strip("\x00\n")
        if not record:
            continue
        sha, _, message = record.partition("\x00")
        count += 1
        for label, excerpt in scan_text(message, needles, redactor):
            hits.append(Hit(where=f"commit {sha.strip()[:12]} message", needle=label,
                            excerpt=excerpt))
    tags = _git(root, "for-each-ref", "--format=%(contents)%x00%(refname)", "refs/tags")
    for chunk in tags.split("\x00"):
        said = chunk.strip()
        if not said:
            continue
        for label, excerpt in scan_text(said, needles, redactor):
            hits.append(Hit(where="a tag message", needle=label, excerpt=excerpt))
    return count, hits


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for at in range(0, len(items), size):
        yield items[at:at + size]


def _objects(root: Path, shas: Sequence[str]) -> Iterable[Tuple[str, str, bytes]]:
    """``(sha, type, content)`` for each sha, straight off ``git cat-file --batch``."""
    wanted = ("\n".join(shas) + "\n").encode("utf-8")
    blob = _git(root, "cat-file", "--batch", data=wanted, text=False)
    at = 0
    while at < len(blob):
        end = blob.find(b"\n", at)
        if end < 0:
            return
        header = blob[at:end].decode("utf-8", "replace").split()
        at = end + 1
        if len(header) < 3:                      # `<sha> missing`: nothing to read
            continue
        sha, kind, size = header[0], header[1], int(header[2])
        yield sha, kind, blob[at:at + size]
        at += size + 1                           # the payload, then git's own newline


# ------------------------------------------------------------------------------------
# The GitHub side: bodies, comments and review comments
# ------------------------------------------------------------------------------------


def _gh_pages(path: str) -> List[Dict[str, Any]]:
    """One ``gh api`` call, paged: every object the endpoint returns.

    ``--jq .[]`` asks gh for one JSON object per line, which pages as a stream rather than as
    one array per page -- a long issue list does not have to arrive as one document.
    """
    command = ["gh", "api", "--paginate", "--jq", ".[]", path]
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError(f"gh api {path} could not run: {exc}") from exc
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise AuditError(f"gh api {path} failed: {said}")
    items: List[Dict[str, Any]] = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:                       # an endpoint that answers one document
            value = None
        if isinstance(value, dict):
            items.append(value)
    return items


#: The three places issue/PR text lives, and what to call each in a report.
GH_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("issues?state=all", "issue"),
    ("issues/comments", "comment"),
    ("pulls/comments", "review comment"),
)


def scan_github(repo: str, needles: Sequence[Needle], redactor: Optional[Redactor] = None,
                fetch: Optional[Callable[[str], List[Dict[str, Any]]]] = None
                ) -> Tuple[int, List[Hit], List[str]]:
    """``(items, hits, problems)`` for issue/PR bodies, comments and review comments.

    *fetch* is how the pages are got -- :func:`_gh_pages` by default -- so a test can hand over
    fake records instead of calling GitHub (this repository is public: the tests use fake
    handles, never a real one).
    """
    get = fetch or _gh_pages
    hits: List[Hit] = []
    problems: List[str] = []
    items = 0
    for suffix, what in GH_SOURCES:
        path = f"repos/{repo}/{suffix}"
        try:
            records = get(path)
        except AuditError as exc:
            problems.append(str(exc))
            continue
        for record in records or []:
            items += 1
            for field_name in ("title", "body", "diff_hunk"):
                text = record.get(field_name)
                if not isinstance(text, str) or not text:
                    continue
                number = record.get("number") or record.get("id") or "?"
                where = f"{what} #{number}" + ("" if field_name == "body"
                                               else f" ({field_name})")
                for label, excerpt in scan_text(text, needles, redactor):
                    hits.append(Hit(where=where, needle=label, excerpt=excerpt))
    return items, hits, problems


# ------------------------------------------------------------------------------------
# One run
# ------------------------------------------------------------------------------------


def audit(repo: str, needles: Sequence[Needle], *, clone: Optional[Path] = None,
          url: Optional[str] = None, github: bool = True, fetch: bool = True,
          reader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
          redactor: Optional[Redactor] = None, keep: bool = False) -> Report:
    """Scan a public repository for the configured values, and say what was found (#1).

    *clone* reuses (and fetches) a mirror clone at that path; without it one is made in a
    temporary directory and removed afterwards unless *keep*. *url* defaults to the
    repository's own clone URL on GitHub, so ``repo`` is all a caller usually gives.
    """
    report = Report(repo=repo)
    scratch: Optional[Path] = None
    try:
        try:
            if clone is None:
                scratch = Path(tempfile.mkdtemp(prefix="audit-"))
                scratch.rmdir()                  # `git clone` wants to make the folder itself
            root = mirror_clone(url or f"https://github.com/{repo}.git",
                                clone or scratch, fetch=fetch)
        except AuditError as exc:
            report.problems.append(str(exc))
            return report
        try:
            report.refs = refs_of(root)
            if report.refs:                      # an empty clone holds nothing to leak
                report.blobs, _files, report.hits = scan_refs(root, needles, redactor)
                report.commits, commit_hits = scan_commits(root, needles, redactor)
                report.hits = [*report.hits, *commit_hits]
        except AuditError as exc:
            report.problems.append(str(exc))
        if github:
            scanned, hits, problems = scan_github(repo, needles, redactor, fetch=reader)
            report.items, report.problems = scanned, [*report.problems, *problems]
            report.hits = [*report.hits, *hits]
        return report
    finally:
        if scratch is not None and not keep:
            shutil.rmtree(scratch, ignore_errors=True)


def render(report: Report, *, verbose: bool = True) -> List[str]:
    """The report as lines: one per hit (most useful first), then what could not be scanned."""
    lines = [str(hit) for hit in report.hits] if verbose else \
        [f"{hit.needle} in {hit.where}" for hit in report.hits]
    lines.extend(f"not scanned: {said}" for said in report.problems)
    lines.append(report.summary())
    return lines


def repo_from_remote(path: Path) -> str:
    """``owner/name`` of a checkout's ``origin``, or ``""`` -- the audit's own convenience."""
    try:
        url = _git(path, "remote", "get-url", "origin").strip()
    except AuditError:
        return ""
    match = re.search(r"github\.com[:/]+([^/]+/[^/\s]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else ""
