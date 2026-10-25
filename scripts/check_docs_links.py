#!/usr/bin/env python3
"""Check relative links and #anchors in the repository's Markdown files.

Usage: python scripts/check_docs_links.py [ROOT]

Every ``[text](target)`` whose target is not a URL (or is a URL of this repository's own
files on GitHub, as README.md uses so its links also work on PyPI) must name a file or directory
that exists, and a ``#fragment`` must match a heading in the target (GitHub's
slug rules). External links are not fetched. Exits 1 and lists each broken link.
"""

import re
import subprocess
import sys
from pathlib import Path

LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
SKIP_PREFIXES = (".agents/inbox/",)
#: Links to this repository's own files on GitHub are checked like relative links.
OWN_URL = re.compile(r"^https://github\.com/eeftychiou/swarmly/(?:blob|tree)/main/(.+)$")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower-case, punctuation dropped, spaces to dashes."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set:
    found, seen, fenced = set(), {}, False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            base = slug(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    for explicit in re.findall(r'<a\s+(?:name|id)="([^"]+)"', path.read_text(encoding="utf-8")):
        found.add(explicit)
    return found


def links(path: Path):
    fenced = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = re.sub(r"`[^`]*`", "", line)
        for target in LINK.findall(line):
            yield lineno, target


def markdown_files(root: Path):
    try:
        out = subprocess.run(["git", "ls-files", "-z", "*.md"], cwd=root,
                             capture_output=True, check=True).stdout.decode("utf-8")
        names = [n for n in out.split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(root)) for p in root.rglob("*.md")]
    return [root / n for n in sorted(names) if not n.startswith(SKIP_PREFIXES)]


def check(root: Path) -> list:
    problems = []
    cache = {}
    for md in markdown_files(root):
        for lineno, target in links(md):
            own = OWN_URL.match(target)
            if own:                       # README.md links absolutely so PyPI renders them
                file_part, _, fragment = own.group(1).partition("#")
                dest = (root / file_part).resolve()
            elif re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):     # http:, mailto:, ...
                continue
            else:
                file_part, _, fragment = target.partition("#")
                dest = (md.parent / file_part).resolve() if file_part else md
            where = f"{md.relative_to(root)}:{lineno}: {target}"
            if not dest.exists():
                problems.append(f"{where} (no such file)")
                continue
            if fragment and dest.suffix == ".md":
                if dest not in cache:
                    cache[dest] = anchors(dest)
                if fragment.lower() not in cache[dest]:
                    problems.append(f"{where} (no heading #{fragment})")
    return problems


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0] if args else Path(__file__).resolve().parent.parent).resolve()
    problems = check(root)
    for problem in problems:
        print(problem)
    print(f"{len(problems)} broken link(s)" if problems else "all relative links resolve")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
