"""Resolve the live Antigravity session endpoint (issue #85, mode-5 wedge).

The defect this module exists to close
-------------------------------------

``agentapi send-message`` delivers a message straight into an open Antigravity
conversation, and it needs ``ANTIGRAVITY_LS_ADDRESS`` to do it. That value names
a **per-process, randomly assigned** port on the running ``language_server`` --
it rotates every time the IDE starts, so it cannot be pinned in an env file and
it is absent from any shell the IDE did not itself spawn.

Measured on this host (2026-09-21): a transport launched by Task Scheduler has
no ``ANTIGRAVITY_LS_ADDRESS``, so every ``send-message`` exits 1 with
``{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}`` and every addressed message
fell through to a blocking ``agy -p`` run (budget ``timeout_seconds``, 1500s)
that held the channel's turn lock, so later messages were dropped while it ran.
15 ``turn already in progress`` drops and 11 ``agentapi queue failed`` warnings
in a single afternoon, with the transport's heartbeat reporting healthy.

Since the port is discoverable deterministically, the absence of the variable
is a *recoverable* condition, not a fact of life. This module recovers it.

How
---

``language_server`` announces its own ports in its startup banner::

    server.go:628] Language server listening on random port at 62933 for HTTPS (gRPC)
    server.go:636] Language server listening on random port at 62934 for HTTP

The **HTTP** port is the one ``agentapi`` wants; the gRPC sibling also accepts a
TCP connection but refuses the call, so it must be ranked second rather than
selected. The banner is the primary source (it names each port's protocol
precisely). ``netstat -ano`` filtered to the ``language_server`` pid is the
fallback for a host whose log has rotated away.

Everything here is stdlib-only on purpose: ``pyproject.toml`` declares no
runtime dependencies, and a discovery mechanism that needed one would not be
deployable on the very host that is broken.

The address is necessary but NOT sufficient
-------------------------------------------

``language_server agentapi`` authenticates every call with a per-session CSRF
token that it also receives on its own command line::

    language_server.exe --standalone ... --csrf_token <uuid> ...

A transport the IDE did not spawn has neither variable, so discovering only the
**address** still fails -- measured on this host 2026-09-21::

    {"error": "rpc error: code = Unauthenticated desc = missing CSRF token"}

and, once a stale token is present, ``invalid CSRF token``. The token cannot be
pinned: it is regenerated on every spawn (69 historical values in the IDE's own
log, only the newest accepted) and is stored in no config file. Recovery is
therefore per-dispatch, from the live process itself -- which is also what makes
it correct across restarts.

Because the token is a **credential**, nothing here ever logs it, returns it in
a ``detail`` string, or includes it in an exception. Only the port and the pid
are ever named.

Nothing in this module ever raises or logs a credential: discovery failing is
an ordinary, expected outcome on a headless host, and it must stay a clean
no-op there (see ``resolve_ls_address``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("swarmly.telegram.endpoint")

#: The variable ``agentapi`` reads. Owned by the IDE, not by us.
LS_ADDRESS_ENV = "ANTIGRAVITY_LS_ADDRESS"

#: The credential ``agentapi`` also requires, from the same per-session spawn.
#: Named "CSRF" by the tool itself; it is a bearer credential for the endpoint.
CSRF_TOKEN_ENV = "ANTIGRAVITY_CSRF_TOKEN"

#: Processes that host a session endpoint. The IDE spawns ``language_server``
#: under its bin directory; the CLI runs its own copy as ``agy``. Measured on
#: this host 2026-09-21: the conversation was served by ``agy`` (port 62989) at
#: 19:00 and by ``language_server`` (port 62776) at 20:40 -- the hosting process
#: changes, so both must be considered or discovery is blind half the time.
_ENDPOINT_PROCESS_NAMES = ("language_server.exe", "agy.exe")

#: Loopback host we always probe with. The IDE binds 127.0.0.1; using the
#: literal rather than ``localhost`` avoids a name-resolution detour (and an
#: IPv6-first ``::1`` attempt) on every probe.
_LOOPBACK = "127.0.0.1"

#: Bound on one probe. The successful case is ~100ms measured; this is a ceiling
#: for a hung socket, not an expectation. Small on purpose: discovery runs on the
#: delivery path, and a slow discovery would recreate the stall it prevents.
_DEFAULT_PROBE_TIMEOUT = 5.0

#: Kept short: the transport marks an endpoint failure and re-probes next time.
#: An endpoint that answered once is worth reusing for a whole run.
_cached_address: Optional[str] = None
_cached_source: str = ""
#: The credential that made the cached address work. Invalidated with the
#: address, because the two rotate together: they come from the same spawn.
_cached_token: str = ""

#: How long a FAILED discovery is remembered. A headless host has nothing to
#: cache, so without this every single message would pay a netstat call plus a
#: probe round trip per candidate -- turning "no IDE here" into a permanent
#: per-message tax. One probe per minute is far below the transport's cadence
#: while still recovering promptly when an IDE appears.
_NEGATIVE_TTL_SECONDS = 60.0
_negative_until: float = 0.0

#: The CIM query is the most expensive thing this module does: measured on the
#: live host 2026-09-21 at ~1.2s (PowerShell start-up, plus a
#: ``Get-NetTCPConnection`` per process), against ~150ms for a probe. A single
#: discovery needs TWO answers from that query -- the credential for the plan and
#: the CLI's ports for the candidate list -- so without this it would pay the
#: cost twice on the delivery path.
#:
#: Cached for seconds, not for the run: pids and ports come and go, and a stale
#: pid list would be worse than a slow one. ``invalidate_cache`` drops it, so a
#: failed delivery re-reads the host immediately.
_CIM_TTL_SECONDS = 5.0
_cim_cached: Optional[List[dict]] = None
_cim_cached_at: float = 0.0


@dataclass(frozen=True)
class EndpointProbe:
    """A candidate address that answered a real ``agentapi`` call."""

    address: str            # "127.0.0.1:62934" -- the form agentapi wants
    port: str
    source: str             # "log" | "netstat" | "discovery" -- where it came from
    latency_ms: int
    detail: str = ""        # bounded, for logs; never a token
    #: The credential that made the call succeed. Excluded from ``repr`` on
    #: purpose: this object is logged and formatted in error paths, and a
    #: credential must never reach a log line or a Telegram message.
    csrf_token: str = field(default="", repr=False)

    def __repr__(self) -> str:      # noqa: D105 - explicit, credential-free
        # Deliberately explicit rather than generated: ``dataclass`` would print
        # the credential, and this object is formatted into log lines and error
        # paths. ``repr=False`` above is the field-level half of the same guard;
        # this method is the half that survives a subclass or a future field.
        token = "***" if self.csrf_token else ""
        return (f"EndpointProbe(address={self.address!r}, port={self.port!r}, "
                f"source={self.source!r}, latency_ms={self.latency_ms}, "
                f"detail={self.detail!r}, csrf_token={token!r})")


def endpoint_error_is_credential(reason: str) -> bool:
    """True when agentapi refused *because of the credential*, not the address.

    Kept here (rather than only in the adapter's marker tuple) so the two causes
    stay distinguishable: a bad credential means the endpoint is KNOWN and the
    remedy is different from "no endpoint at all".
    """
    flat = " ".join(str(reason or "").lower().split())
    return "csrf" in flat or "unauthenticated" in flat


# ---------------------------------------------------------------------------
# Pure parsers (no I/O -- these are what the tests drive)
# ---------------------------------------------------------------------------

_BANNER_RE = re.compile(
    r"listening on random port at (?P<port>\d+) for (?P<proto>HTTPS \(gRPC\)|HTTP)"
)


def ports_from_banner(text: str) -> List[str]:
    """Candidate ports from ``language_server``'s startup banner, best first.

    Ordered by **announcement recency**, newest first. The banner is appended to
    across IDE restarts, so every earlier announcement names a port that is now
    dead -- and probing a dead port costs a full TCP timeout, while the gRPC
    sibling fails fast. Newest-first therefore puts the one address that can work
    first and keeps the expensive failure last.

    Within one process the log emits gRPC before HTTP; reversing the match list
    yields HTTP before its sibling for free, which is the order we want (only
    HTTP serves ``agentapi``; the gRPC port accepts the connection and refuses
    the call).
    """
    matches = list(_BANNER_RE.finditer(text or ""))
    ordered: List[str] = []
    for match in reversed(matches):          # newest announcement wins
        port = match.group("port")
        if port not in ordered:
            ordered.append(port)
    return ordered


_TASKLIST_RE = re.compile(
    r'^\s*"?language_server(?:\.exe)?"?\s*,+\s*"?(\d+)"?', re.IGNORECASE
)


def pids_from_tasklist(text: str) -> List[int]:
    """Pids of every ``language_server`` process, from ``tasklist`` CSV output."""
    pids: List[int] = []
    for line in (text or "").splitlines():
        match = _TASKLIST_RE.match(line)
        if match:
            try:
                pids.append(int(match.group(1)))
            except ValueError:          # pragma: no cover - regex guarantees digits
                continue
    return pids


def ports_from_netstat(text: str, pids: Iterable[int]) -> List[str]:
    """Loopback LISTENING ports owned by one of ``pids``, from ``netstat -ano``.

    Scoped to the language server's own pids on purpose: an unfiltered
    ``netstat`` lists every listener on the host, and probing unrelated services
    would be both slow and a (harmless but noisy) source of false negatives.
    """
    wanted = {str(pid) for pid in pids}
    if not wanted:
        return []
    ports: List[str] = []
    for line in (text or "").splitlines():
        fields = line.split()
        # proto  local  foreign  state  pid
        if len(fields) < 5 or fields[-2].upper() != "LISTENING" or fields[-1] not in wanted:
            continue
        local = fields[1]
        if not (local.startswith(_LOOPBACK + ":") or local.startswith("[::1]:")):
            continue
        match = re.search(r":(\d+)$", local)
        if match and match.group(1) not in ports:
            ports.append(match.group(1))
    return ports


# ---------------------------------------------------------------------------
# Host access (thin, each guarded -- discovery never raises)
# ---------------------------------------------------------------------------

def language_server_log_path() -> Optional[Path]:
    """Path of the language server's own log on this host, if present.

    Windows-only by construction; the IDE's layout is what it is. Returning
    ``None`` is normal and simply disables this source.
    """
    if os.name != "nt":
        return None
    roaming = os.environ.get("APPDATA")
    if not roaming:
        return None
    return Path(roaming) / "Antigravity" / "logs" / "language_server.log"


#: The language server log is append-only and reaches ~10MB on the live host
#: (measured 2026-09-21: 9,540,949 bytes). Reading it whole on the delivery path
#: is not acceptable, so both reads below are bounded.
#:
#: A bounded read needs BOTH ends, which was got wrong once: the banner is written
#: when the process STARTS, so the current process's banner is near the file HEAD.
#: Measured 2026-09-21 on the live host: the only two announcements sat at lines
#: 5-6 of a 12MB file, while a tail window began ~992KB in -- so
#: ``ports_from_banner()`` returned ``[]`` and this source was silently dead. Only
#: ``endpoint_process_listeners()`` was carrying discovery.
#:
#: The tail still matters: across IDE restarts the log accumulates announcements,
#: and the newest one is what names the live port, so ``log_text_from_log`` returns
#: head-then-tail to keep the text in chronological order for
#: ``ports_from_banner``'s newest-first ordering.
_MAX_LOG_TAIL_BYTES = 262144
_MAX_LOG_HEAD_BYTES = 65536


def _read_text(path: Path, max_bytes: int = _MAX_LOG_TAIL_BYTES) -> str:
    """Read at most the last ``max_bytes`` of ``path``; never raises.

    ``errors="replace"`` matters more than usual here: seeking into the middle of
    a file can split a multi-byte character, and the banner shares this file with
    arbitrary log noise. A mangled byte must not discard the port.
    """
    try:
        size = path.stat().st_size
    except (OSError, ValueError):
        return ""
    try:
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read(max_bytes)
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8", errors="replace")


def _read_text_head(path: Path, max_bytes: int = _MAX_LOG_HEAD_BYTES) -> str:
    """Read at most the first ``max_bytes`` of ``path``; never raises.

    Where the CURRENT process's banner lives. See ``_MAX_LOG_TAIL_BYTES`` for why
    a tail-only read is not enough.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes)
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8", errors="replace")


def log_text_from_log(path: Optional[Path]) -> str:
    """Bounded log text for banner parsing: head window then tail window.

    Chronological by construction -- the head holds the earliest lines and the
    tail the latest -- which is what ``ports_from_banner`` relies on to put the
    newest announcement first. A file shorter than the head window is read once.
    """
    if path is None:
        return ""
    try:
        size = path.stat().st_size
    except (OSError, ValueError):
        return ""
    if size <= _MAX_LOG_HEAD_BYTES:
        return _read_text_head(path, _MAX_LOG_HEAD_BYTES + 1)
    return _read_text_head(path) + _read_text(path)


def _run(argv: Sequence[str], timeout: float) -> Tuple[int, str]:
    """Run a discovery helper command, returning ``(rc, stdout)``; never raises."""
    try:
        result = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return result.returncode, result.stdout or ""


def live_language_server_pids() -> List[int]:
    """Pids of running ``language_server`` processes via ``tasklist``."""
    if os.name != "nt":
        return []
    rc, out = _run(["tasklist", "/FI", "IMAGENAME eq language_server.exe",
                    "/FO", "CSV", "/NH"], 10.0)
    return pids_from_tasklist(out) if rc == 0 else []


def loopback_listeners(pids: Sequence[int]) -> List[str]:
    """Loopback LISTENING ports for ``pids`` via ``netstat -ano``."""
    if os.name != "nt" or not pids:
        return []
    rc, out = _run(["netstat", "-ano", "-p", "TCP"], 15.0)
    return ports_from_netstat(out, pids) if rc == 0 else []


# ---------------------------------------------------------------------------
# Credential recovery (the address is not enough -- see the module docstring)
# ---------------------------------------------------------------------------

#: ``--csrf_token <uuid>`` as the IDE spells it. Both ``--csrf_token value`` and
#: ``--csrf_token=value`` are accepted: the measured live host used a space, and
#: the flag parser accepts either, so a future IDE build may switch.
_CSRF_ARG_RE = re.compile(
    r"--csrf_token[=\s]+(?P<token>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def csrf_token_from_command_line(text: str) -> Optional[str]:
    """The **last** ``--csrf_token`` value in one command line, or ``None``.

    Last, not first: the flag is single-valued, and taking the last match keeps
    the same "newest wins" rule the banner parser uses if a build ever repeats
    it. Returns ``None`` for anything unparseable -- never raises.
    """
    matches = list(_CSRF_ARG_RE.finditer(text or ""))
    if not matches:
        return None
    return matches[-1].group("token")


#: One WMI query for every endpoint-hosting process. This is the ONLY place the
#: credential exists outside the live process: it is written to no file, and no
#: environment the IDE did not create contains it. ``Get-CimInstance`` is used
#: rather than a dependency because this module is stdlib-only by design.
#:
#: JSON is emitted because a Windows command line can contain any delimiter we
#: might pick (including ``|`` and spaces), so positional splitting is unsafe.
#: ``@($out)`` forces an array even for a single process, which ``ConvertTo-Json``
#: would otherwise collapse to a bare object.
_CIM_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($n in @(__NAMES__)) {
  foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='$n'")) {
    $ports = @(Get-NetTCPConnection -State Listen -OwningProcess $p.ProcessId |
               Where-Object { $_.LocalAddress -eq '127.0.0.1' } |
               Select-Object -ExpandProperty LocalPort)
    $out += [pscustomobject]@{ pid = $p.ProcessId; cmd = $p.CommandLine; ports = $ports }
  }
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 4
"""


def _cim_endpoint_processes() -> List[dict]:
    """Live endpoint-hosting processes as ``[{pid, cmd, ports}, ...]``.

    Best-effort: any failure yields ``[]``, because a host without the IDE (or
    without PowerShell) must stay a clean no-op rather than an error.

    Memoised for ``_CIM_TTL_SECONDS`` because one discovery asks this twice (the
    credential and the ports) and the query costs ~1.2s measured.
    """
    global _cim_cached, _cim_cached_at
    if os.name != "nt":
        return []
    now = time.time()
    if _cim_cached is not None and (now - _cim_cached_at) < _CIM_TTL_SECONDS:
        return _cim_cached
    names = ",".join("'%s'" % n for n in _ENDPOINT_PROCESS_NAMES)
    rc, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    _CIM_SCRIPT.replace("__NAMES__", names)], 20.0)
    if rc != 0 or not (out or "").strip():
        return []
    try:
        parsed = json.loads(out)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, dict):        # single process: ConvertTo-Json unwrapped
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    _cim_cached = [item for item in parsed if isinstance(item, dict)]
    _cim_cached_at = now
    return _cim_cached


def csrf_token_candidates() -> List[str]:
    """Distinct CSRF tokens from live endpoint processes, newest pid last.

    Ordering mirrors ``ports_from_banner``: later spawns are appended later, and
    a superseded token is rejected by the server anyway (``invalid CSRF token``),
    which costs one fast round trip rather than a wrong answer.
    """
    tokens: List[str] = []
    for item in _cim_endpoint_processes():
        token = csrf_token_from_command_line(item.get("cmd") or "")
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def endpoint_process_listeners() -> List[str]:
    """Loopback ports of live endpoint processes, IDE and CLI alike.

    Needed because ``live_language_server_pids`` covers only the IDE's
    ``language_server``; when the CLI (``agy``) hosts the session its ports are
    owned by a different image and would otherwise never be probed.
    """
    ports: List[str] = []
    for item in _cim_endpoint_processes():
        for port in item.get("ports") or []:
            text = str(port)
            if text.isdigit() and text not in ports:
                ports.append(text)
    return ports


def candidate_ports(log_text: Optional[str] = None,
                    netstat_text: Optional[str] = None,
                    pids: Optional[Sequence[int]] = None) -> List[str]:
    """Merged, de-duplicated candidate ports: banner-derived first.

    ``log_text``/``netstat_text``/``pids`` are injected by tests; on the host
    they are read here. The banner leads because it states each port's protocol;
    netstat contributions are appended as a fallback tier.
    """
    if log_text is None:
        log_text = log_text_from_log(language_server_log_path())

    ordered = ports_from_banner(log_text)
    if netstat_text is None:
        if pids is None:
            pids = live_language_server_pids()
        net_ports = loopback_listeners(pids)
        # The CLI hosts its own session when ``agy`` is running, and it is not a
        # ``language_server`` image, so netstat on that pid alone misses it.
        for port in endpoint_process_listeners():
            if port not in net_ports:
                net_ports.append(port)
    else:
        net_ports = ports_from_netstat(netstat_text, pids or ())
    for port in net_ports:
        if port not in ordered:
            ordered.append(port)
    return ordered


def probe(agentapi_cmd: Sequence[str], conversation_id: str, port: str,
          timeout: float = _DEFAULT_PROBE_TIMEOUT,
          csrf_token: str = "") -> Tuple[bool, str, int]:
    """Ask a candidate port a real ``agentapi`` question: ``(ok, detail, ms)``.

    ``get-conversation-metadata`` is used deliberately: it is read-only, it
    starts no model turn, it posts nothing to Telegram, and unlike a bare TCP
    connect it discriminates the HTTP port from its gRPC sibling (which accepts
    the connection and then refuses the call).

    ``csrf_token`` is injected when supplied. The token is NEVER included in the
    returned ``detail`` -- that string is logged and surfaced to Telegram, so a
    refused credential is reported by its condition ("csrf rejected"), which is
    the actionable part, and not by its value.
    """
    argv = [*agentapi_cmd, "get-conversation-metadata", conversation_id]
    env = dict(os.environ)
    env[LS_ADDRESS_ENV] = f"{_LOOPBACK}:{port}"
    if csrf_token:
        env[CSRF_TOKEN_ENV] = csrf_token
    started = time.time()
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
        output = result.stdout or ""
        elapsed = int((time.time() - started) * 1000)
        ok = result.returncode == 0 and "conversationMetadata" in output
        if ok:
            return True, f"rc={result.returncode}", elapsed
        if endpoint_error_is_credential(output):
            # Distinguishable from a dead port: the endpoint answered, it just
            # refused this credential. Worth naming separately -- the remedies
            # are different.
            return False, "credential rejected (csrf)", elapsed
        return False, f"rc={result.returncode}", elapsed
    except subprocess.TimeoutExpired:
        return False, f"probe timed out after {timeout:g}s", int(timeout * 1000)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"probe error: {exc.__class__.__name__}", int(
            (time.time() - started) * 1000)


def _probe_candidates(agentapi_cmd: Sequence[str], conversation_id: str,
                      ports: Sequence[str],
                      csrf_token: str) -> Optional[EndpointProbe]:
    """First ``port`` that answers with ``csrf_token``; ``None`` if none does."""
    for port in ports:
        ok, detail, ms = probe(agentapi_cmd, conversation_id, port,
                               csrf_token=csrf_token)
        if ok:
            return EndpointProbe(address=f"{_LOOPBACK}:{port}", port=port,
                                 source="discovery", latency_ms=ms,
                                 detail=detail, csrf_token=csrf_token)
        logger.debug("endpoint: candidate port %s did not answer (%s)",
                     port, detail)
    return None


def discover(agentapi_cmd: Optional[Sequence[str]],
             conversation_id: Optional[str], *,
             log_text: Optional[str] = None,
             netstat_text: Optional[str] = None,
             pids: Optional[Sequence[int]] = None) -> Optional[EndpointProbe]:
    """Find a candidate port that actually answers; ``None`` when none does.

    The return value is *measured*, never assumed: a port is only reported after
    an ``agentapi`` call against it succeeded. That is what keeps a wrong port
    from being handed to the delivery path.

    Credentials are discovered and tried too, because a correct port with no
    credential fails exactly as a dead port does -- measured 2026-09-21, a
    scheduler-launched transport reached the correct port and still got
    ``missing CSRF token`` on every dispatch, which is what made a working
    endpoint look like a broken one.

    The two passes are deliberately ordered and the second is *lazy*:

    1. the caller's own environment (usually no credential, but an IDE-spawned
       transport already has one) -- costs what discovery cost before;
    2. the credential read off the live process -- reached only when pass 1
       failed, so the ~1.2s host query is paid only by the process that actually
       needs it, and never per message (the result is cached by the caller).
    """
    if not agentapi_cmd or not conversation_id:
        return None
    ports = candidate_ports(log_text, netstat_text, pids)
    if not ports:
        return None

    found = _probe_candidates(agentapi_cmd, conversation_id, ports, "")
    if found is not None:
        return found

    for token in csrf_token_candidates():
        found = _probe_candidates(agentapi_cmd, conversation_id, ports, token)
        if found is not None:
            logger.info(
                "endpoint: recovered the session credential from the live "
                "process (port %s, %dms)", found.port, found.latency_ms,
            )
            return found
    return None


def resolve_endpoint(agentapi_cmd: Optional[Sequence[str]],
                     conversation_id: Optional[str], *,
                     environ: Optional[Dict[str, str]] = None,
                     use_cache: bool = True) -> Optional[EndpointProbe]:
    """The endpoint AND credential ``agentapi`` should use, or ``None``.

    The credential-aware sibling of :func:`resolve_ls_address`. Callers that must
    inject both variables use this; callers that only need the address keep using
    ``resolve_ls_address`` (which is now used only by tests -- the delivery path
    cannot work from an address alone).

    Order of preference:

    1. an operator-set **pair** (address + token) -- an explicit choice is always
       respected and must not cost a probe;
    2. the cached result of an earlier successful discovery;
    3. an operator-set address WITHOUT a token: the address is still respected
       (including when it is wrong), and only the credential is recovered;
    4. a fresh full discovery.

    Tier 3 matters because an address without a credential is not usable: on the
    live host the IDE-inherited transport had a correct
    ``ANTIGRAVITY_LS_ADDRESS`` and still failed every dispatch with
    ``missing CSRF token``.

    Returning ``None`` is the supported headless case: no IDE, no endpoint, and
    the caller degrades exactly as it did before this module existed.
    """
    global _cached_address, _cached_source, _negative_until, _cached_token
    env = os.environ if environ is None else environ
    existing = (env.get(LS_ADDRESS_ENV) or "").strip()
    existing_token = (env.get(CSRF_TOKEN_ENV) or "").strip()

    # 1. A complete operator-set pair: respected, and free.
    if existing and existing_token:
        return EndpointProbe(address=existing, port=existing.rsplit(":", 1)[-1],
                             source="environment", latency_ms=0,
                             csrf_token=existing_token)

    # 2. A previously successful discovery, cached for this run.
    if use_cache and _cached_address:
        return EndpointProbe(address=_cached_address,
                             port=_cached_address.rsplit(":", 1)[-1],
                             source=_cached_source, latency_ms=0,
                             csrf_token=_cached_token)

    # 3. An address without a credential. Port discovery is NOT run -- the
    #    operator's address wins -- but a credential is still required, so the
    #    live processes are queried for one that this address accepts.
    if existing:
        port = existing.rsplit(":", 1)[-1]
        if not agentapi_cmd or not conversation_id:
            # Nothing to probe with: the address is returned as-is and the
            # credential is left to the caller. No host query is attempted,
            # because there would be no way to test an answer.
            return EndpointProbe(address=existing, port=port,
                                 source="environment", latency_ms=0)
        if use_cache and time.time() < _negative_until:
            # A recent search already established that no live credential is
            # accepted by this address. Re-running PowerShell per message to
            # learn the same thing is the tax this record exists to remove.
            return EndpointProbe(address=existing, port=port,
                                 source="environment", latency_ms=0)
        for token in csrf_token_candidates():
            ok, detail, ms = probe(agentapi_cmd, conversation_id, port,
                                   csrf_token=token)
            if ok:
                _cached_address, _cached_source, _cached_token = (
                    existing, "environment", token)
                _negative_until = 0.0
                logger.info(
                    "endpoint: recovered the session credential for the "
                    "operator-set address %s (%dms) -- agentapi will use the "
                    "fast path", existing, ms,
                )
                return EndpointProbe(address=existing, port=port,
                                     source="environment", latency_ms=ms,
                                     csrf_token=token)
            logger.debug("endpoint: operator address %s refused a credential "
                         "candidate (%s)", existing, detail)
        # Every candidate refused. Fall through with the address intact and no
        # credential: the caller reports the real reason rather than silently
        # substituting a different endpoint.
        _negative_until = time.time() + _NEGATIVE_TTL_SECONDS
        return EndpointProbe(address=existing, port=port,
                             source="environment", latency_ms=0)

    # 4. Nothing set: full discovery, which recovers the credential too.
    if use_cache and time.time() < _negative_until:
        return None
    found = discover(agentapi_cmd, conversation_id)
    if found is None:
        _negative_until = time.time() + _NEGATIVE_TTL_SECONDS
        return None
    _cached_address = found.address
    _cached_source = found.source
    _cached_token = found.csrf_token
    _negative_until = 0.0
    logger.info(
        "endpoint: resolved the live session endpoint by discovery "
        "(port %s, %dms, credential %s) -- agentapi will use the fast path",
        found.port, found.latency_ms,
        "recovered" if found.csrf_token else "inherited",
    )
    return found


def resolve_ls_address(agentapi_cmd: Optional[Sequence[str]],
                       conversation_id: Optional[str], *,
                       environ: Optional[Dict[str, str]] = None,
                       use_cache: bool = True) -> Optional[str]:
    """The address ``agentapi`` should use, or ``None`` if none is recoverable.

    Address-only, and deliberately so: it performs NO credential discovery, so
    an operator-set address costs nothing and a caller that only needs the port
    does not pay for a credential probe.

    Address-only also means it is NOT sufficient to dispatch: ``agentapi`` needs
    the per-session credential as well, so the delivery path uses
    :func:`resolve_endpoint` instead (see the module docstring). This remains the
    diagnostic/address-only entry point -- e.g. for reporting which port is live
    -- and it keeps the original caching contract: one probe per run, and a
    failed discovery remembered briefly so a headless host is not re-probed on
    every message.

    Returning ``None`` is the supported headless case: no IDE, no endpoint, and
    the caller degrades exactly as it did before this module existed.
    """
    global _cached_address, _cached_source, _negative_until
    env = os.environ if environ is None else environ
    existing = (env.get(LS_ADDRESS_ENV) or "").strip()
    if existing:
        return existing
    if use_cache:
        if _cached_address:
            return _cached_address
        if time.time() < _negative_until:
            # Recent discovery already established there is nothing to find.
            return None
    found = discover(agentapi_cmd, conversation_id)
    if found is None:
        _negative_until = time.time() + _NEGATIVE_TTL_SECONDS
        return None
    _cached_address = found.address
    _cached_source = found.source
    _negative_until = 0.0
    return found.address


def invalidate_cache() -> None:
    """Drop the cached address, credential AND negative result; force a re-probe.

    Called when a delivery attempt fails: the port and the token belong to a
    process that may have been restarted (or the IDE may have closed), and a
    stale pair would otherwise be reused for the life of the transport. The
    negative record is cleared too -- a failure is exactly the evidence that the
    situation may have changed, so retrying immediately is the point.

    The memoised host query is dropped as well, for the same reason: after a
    failure the pid/port snapshot is precisely the thing suspected of being
    stale, and re-reading it is the whole point of the retry.
    """
    global _cached_address, _cached_source, _negative_until, _cached_token
    global _cim_cached, _cim_cached_at
    _cached_address = None
    _cached_source = ""
    _cached_token = ""
    _negative_until = 0.0
    _cim_cached = None
    _cim_cached_at = 0.0
