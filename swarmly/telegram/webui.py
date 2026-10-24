"""The live swarm dashboard as a local web page (issue #240, slice 1).

The pinned Telegram card (#117, #241) answers "what is the swarm doing" in one message that
is edited in place. This module answers the same question in a page: the transport that holds
the lease serves it on the loopback, and the page polls one small JSON endpoint every few
seconds. No model turn is involved on either side.

Three rules decide the shape, the same three the card keeps.

**A status, not a leak.** Every field here is picked by *name* out of the records the
transport already holds, so a snapshot or usage row carrying a chat id, a thread id, a token
or a path shows none of them. A discussion appears as its ``tg-…`` token, never an id. The
page has no external references at all -- no CDN, no font host, no analytics -- so it works on
a LAN with no route out, and nothing in it can call home.

**Unknown stays unknown.** A number the runtime never reported is ``null`` in the JSON and
``?`` on the page; it is never summed as a zero. A total says how many rows reported one.

**Read-only, and only while this bot holds the lease.** The server answers ``GET``/``HEAD``
for two paths and refuses everything else: the admin buttons stay in Telegram. It binds
``127.0.0.1``, so nothing off the host can reach it; slice 2 (a Telegram Mini App over HTTPS)
is where an address becomes a decision, and that decision is the admin's.

The page itself is one string with its styles and script inline -- self-contained, phone-width
first, light and dark by ``prefers-color-scheme`` -- ready to be the Mini App's page unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import parse_qsl

from swarmly.telegram.snapshot import (BUSY, DOWN, FROZEN, IDLE, PAUSED, STUCK, Snapshot)

#: One word per state, and the colour class the page paints it with (#241's vocabulary).
BADGES: Dict[str, str] = {
    BUSY: "working", IDLE: "idle", PAUSED: "paused", FROZEN: "frozen",
    STUCK: "stuck", DOWN: "down",
}

#: The two paths the server knows. Anything else is a 404, and no other method is allowed.
PAGE_PATH = "/"
STATE_PATH = "/state.json"

#: Where the server binds. Loopback only: the page is reached through a tunnel, not a bind
#: (#240 slice 2). The bind stays local so nothing on the network can talk to it directly.
HOST = "127.0.0.1"

logger = logging.getLogger(__name__)

#: The ``Host`` values that are always allowed. A request naming anything else is refused
#: unless the operator configured that name -- see :func:`allowed_hosts` and the DNS-rebinding
#: note in :class:`DashboardServer`.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

#: The header a Mini App's page sends its ``initData`` in, and how long a known-good one lasts.
INIT_HEADER = "X-Telegram-Init-Data"
INIT_MAX_AGE = 300.0


# ------------------------------------------------------------------------------------
# Reading the records
# ------------------------------------------------------------------------------------


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _sum_or_none(values: Sequence[Optional[float]]) -> Optional[float]:
    """The sum of what was reported, or ``None`` when nothing was: never a made-up zero."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _spend(usage: Optional[Mapping[str, Any]], now: float) -> Dict[str, Any]:
    """One bot's spend today: numbers where reported, ``None`` where the runtime was silent."""
    usage = usage or {}
    quota_left = usage.get("quota_left")
    resets = usage.get("quota_resets_at")
    return {
        "turns": None if usage.get("turns") is None else int(_num(usage.get("turns"))),
        "tokens_in": (None if usage.get("tokens_in") is None
                      else int(_num(usage.get("tokens_in")))),
        "tokens_out": (None if usage.get("tokens_out") is None
                       else int(_num(usage.get("tokens_out")))),
        "cost": None if usage.get("cost_usd") is None else round(_num(usage.get("cost_usd")), 4),
        "quota_left": None if quota_left is None else int(_num(quota_left)),
        "quota_resets_in": (max(0.0, _num(resets) - now)
                            if quota_left is not None and resets else None),
    }


def bot_cards(*, now: float, snapshots: Sequence[Snapshot] = (),
              usage: Optional[Mapping[str, Mapping[str, Any]]] = None,
              roles: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """One card per bot, in alias order: what it is, what it is doing, what it spent.

    A record that stopped being renewed is ``down`` whatever it last said of itself (#124):
    a dead bot publishes nothing, so the age of its record is the only thing that can say it.
    """
    moment = float(now)
    usage = usage or {}
    roles = roles or {}
    newest: Dict[str, Snapshot] = {}
    for row in snapshots:
        key = str(row.alias).strip().lower()
        if key not in newest or row.at >= newest[key].at:
            newest[key] = row
    cards: List[Dict[str, Any]] = []
    for key in sorted(newest):
        row = newest[key]
        state = DOWN if (row.state == DOWN or row.is_stale(moment)) else row.state
        since = _num(row.since)
        working = state == BUSY or state == STUCK
        card: Dict[str, Any] = {
            "alias": str(row.alias),
            "role": (roles.get(row.alias) or roles.get(key) or "").strip() or None,
            "state": state,
            "badge": BADGES.get(state, "?"),
            # When the *state* began. For a bot that stopped beating that is the record's
            # age, because its own "since" is as old as the last thing it managed to say.
            "since": (max(0.0, row.age(moment)) if state == DOWN
                      else (max(0.0, moment - since) if since and since <= moment else None)),
            "model": str(row.model or "").strip() or None,
            "effort": str(row.effort or "").strip() or None,
            "rev": str(row.rev or "").strip() or None,
            "topic": str(row.topic or "").strip() or None,
            "turn_seconds": (max(0.0, _num(row.turn_elapsed)) if working else None),
            "turn_timeout": (_num(row.turn_timeout) if working and row.turn_timeout else None),
            "pulse_age": row.age(moment),
            "queue": int(_num(row.queue)),
            "wakes": len(row.wakes or ()),
        }
        card.update(_spend(usage.get(row.alias), moment))
        cards.append(card)
    return cards


def swarm_total(cards: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The swarm's day: the sum of what the cards reported, and how many cards reported it."""
    def column(name: str) -> List[Optional[float]]:
        return [card.get(name) for card in cards]

    turns = column("turns")
    return {
        "bots": len(cards),
        "reported_turns": sum(1 for value in turns if value is not None),
        "turns": None if not any(value is not None for value in turns)
        else int(sum(_num(value) for value in turns if value is not None)),
        "tokens_in": _sum_or_none([value for value in column("tokens_in")]),
        "tokens_out": _sum_or_none([value for value in column("tokens_out")]),
        "cost": _sum_or_none([value for value in column("cost")]),
    }


def owed_rows(rows: Sequence[Any] = (), *, now: float) -> List[Dict[str, Any]]:
    """The open expectations, read by name: who owes what, to whom, for how long (#162)."""
    moment = float(now)
    out: List[Dict[str, Any]] = []
    for row in rows:
        opened = _num(getattr(row, "created_at", 0.0))
        deadline = _num(getattr(row, "deadline", 0.0))
        out.append({
            "item": str(getattr(row, "item", "") or "") or None,
            "kind": str(getattr(row, "kind", "") or "") or None,
            "debtor": str(getattr(row, "debtor", "") or "") or None,
            "owes": str(getattr(row, "owes", "") or "") or None,
            "waiting_on": str(getattr(row, "waiting_on", "") or "") or None,
            "head": (str(getattr(row, "head", "") or "") or "")[:7] or None,
            "topic": str(getattr(row, "topic", "") or "") or None,
            "flag": str(getattr(row, "flag", "") or "") or None,
            "stalls": int(_num(getattr(row, "stalls", 0))),
            # How long the debt has stood, and how long is left before its deadline is late.
            "open_seconds": max(0.0, moment - opened) if opened else None,
            "due_in": (deadline - moment) if deadline else None,
        })
    return out


def watch_state(*, now: float, snapshots: Sequence[Snapshot] = (),
                usage: Optional[Mapping[str, Mapping[str, Any]]] = None,
                roles: Optional[Mapping[str, str]] = None, coordinator: str = "",
                lease: Optional[Any] = None, round_state: Optional[Any] = None,
                owed: Sequence[Any] = ()) -> Dict[str, Any]:
    """The JSON the page polls: the same records the pinned card is built from, in full.

    The card drops detail to fit 4,096 characters; a page has room, so nothing is cut here --
    but nothing is looked up either: this is a view over records the transport already took.
    """
    moment = float(now)
    cards = bot_cards(now=moment, snapshots=snapshots, usage=usage, roles=roles)
    # The lease is the authority (#83): a name the caller remembers can be stale, the folded
    # lease record cannot be older than the term it carries.
    holder = (str(getattr(lease, "holder", "") or "").strip()
              or str(coordinator or "").strip() or None)
    expires = _num(getattr(lease, "expires_at", 0.0))
    items = _round_items(round_state)
    done = sum(1 for item in items if item["state"] in ("merged", "closed", "done"))
    return {
        "at": moment,
        "refreshed": time.strftime("%H:%M:%SZ", time.gmtime(moment)),
        "coordinator": holder,
        "lease": {
            "holder": holder,
            "term": int(_num(getattr(lease, "term", 0))) or None,
            "expires_in": (max(0.0, expires - moment) if expires else None),
            "status": str(getattr(lease, "status", "") or "") or None,
        },
        "round": ({"name": str(getattr(round_state, "round", "") or "") or None,
                   "status": str(getattr(round_state, "status", "") or "") or None,
                   "done": done, "total": len(items),
                   "items": items}
                  if round_state is not None else None),
        "bots": cards,
        "total": swarm_total(cards),
        "owed": owed_rows(owed, now=moment),
    }


def _round_items(round_state: Optional[Any]) -> List[Dict[str, Any]]:
    """The round's items, by name: the checklist progress is all the strip needs (#240)."""
    items = getattr(round_state, "items", None)
    if not isinstance(items, dict):
        return []
    out: List[Dict[str, Any]] = []
    for key, raw in items.items():
        if not isinstance(raw, dict):
            continue
        out.append({
            "item": str(key),
            "state": str(raw.get("state") or "").strip().lower(),
            "owner": str(raw.get("owner") or "") or None,
            "pr": raw.get("pr") if isinstance(raw.get("pr"), int) else None,
        })
    return out


# ------------------------------------------------------------------------------------
# Who may read it (slice 2: the Telegram Mini App)
# ------------------------------------------------------------------------------------


def allowed_hosts(*, public_host: str = "") -> List[str]:
    """The ``Host`` values the server answers for: the loopback, plus the configured one.

    Binding the loopback is not enough on its own: a page served from a name that *resolves*
    to ``127.0.0.1`` is same-origin with this one, so its script could read ``/state.json``.
    Refusing every ``Host`` but these is what stops it (review, #256 — DNS rebinding).
    """
    hosts = list(LOOPBACK_HOSTS)
    public = str(public_host or "").strip().lower()
    if public:
        hosts.append(public)
        public_host_only = public.split(":", 1)[0]
        hosts.append(public_host_only)
    return hosts


def verify_init_data(raw: str, *, bot_token: str, admin_id: Any = 0,
                     now: Optional[float] = None, max_age: float = INIT_MAX_AGE
                     ) -> tuple[bool, str]:
    """``(ok, why not)`` for a Telegram Mini App's ``initData`` string.

    The check is Telegram's own (Web App docs): the payload is ``key=value`` pairs, and the
    ``hash`` field is ``HMAC-SHA256`` of the rest -- sorted, newline-joined -- under a secret
    that is itself ``HMAC-SHA256`` of the bot token keyed by ``"WebAppData"``. On top of that
    this refuses a signature older than *max_age* (a replayed one), one dated in the future,
    and any user that is not the ``admin_user_id``: the page is the admin's, so a member who
    somehow holds a valid payload still reads nothing.
    """
    moment = time.time() if now is None else float(now)
    text = str(raw or "").strip()
    if not text:
        return False, "no init data"
    if not bot_token:
        return False, "no bot token to check it against"
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=False)
    fields = dict(pairs)
    given = fields.pop("hash", "")
    if not given:
        return False, "no hash in the payload"
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        return False, "the hash does not match the payload"
    try:
        signed_at = float(fields.get("auth_date") or "")
    except (TypeError, ValueError):
        return False, "no auth_date"
    if max_age > 0 and moment - signed_at > max_age:
        return False, "the payload is older than %ds" % int(max_age)
    if signed_at - moment > 60:
        return False, "the payload is dated in the future"
    try:
        user = json.loads(fields.get("user") or "{}")
    except ValueError:
        return False, "the user field is unreadable"
    wanted = int(admin_id or 0)
    if not wanted:
        return False, "no admin_user_id to check against"
    if int(user.get("id") or 0) != wanted:
        return False, "not the admin"
    return True, "ok"


# ------------------------------------------------------------------------------------
# The page
# ------------------------------------------------------------------------------------


def page_html(*, title: str = "Swarm", every: float = 3.0) -> str:
    """The whole page: styles, markup and script inline, nothing fetched from anywhere else."""
    return (_PAGE
            .replace("__TITLE__", _escape(title))
            .replace("__EVERY_MS__", str(int(max(1.0, float(every)) * 1000)))
            .replace("__STATE_PATH__", STATE_PATH))


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>__TITLE__</title>
<style>
:root {
  --bg: #f6f7f9; --card: #fff; --ink: #14181f; --dim: #5b6472; --line: #e3e6ea;
  --working: #2f6fed; --idle: #1f9d55; --paused: #8a93a0; --frozen: #6f5bd3;
  --stuck: #c47f00; --down: #cf2f2f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1216; --card: #171c22; --ink: #e8ecf1; --dim: #9aa4b2; --line: #262d36;
    --working: #6f9dff; --idle: #4fd18b; --paused: #98a2b0; --frozen: #a795ff;
    --stuck: #f0b429; --down: #ff6b6b;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 12px; background: var(--bg); color: var(--ink);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
h1 { font-size: 17px; margin: 0 0 2px; }
.sub { color: var(--dim); font-size: 13px; margin-bottom: 12px; }
.strip, .card, .owed { background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 10px 12px; margin-bottom: 10px; }
.strip b { font-weight: 600; }
.strip .row { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 13px; color: var(--dim); }
.card .top { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.alias { font-weight: 600; }
.badge { font-size: 12px; font-weight: 600; padding: 1px 8px; border-radius: 999px;
  border: 1px solid currentColor; }
.badge.working { color: var(--working); } .badge.idle { color: var(--idle); }
.badge.paused { color: var(--paused); } .badge.frozen { color: var(--frozen); }
.badge.stuck { color: var(--stuck); } .badge.down { color: var(--down); }
.card .doing { margin-top: 4px; font-size: 14px; }
.card .meta { margin-top: 4px; color: var(--dim); font-size: 13px;
  display: flex; flex-wrap: wrap; gap: 2px 12px; }
.owed h2, .strip h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--dim); margin: 0 0 6px; }
.owed .line { font-size: 13px; padding: 3px 0; border-top: 1px solid var(--line); }
.owed .line:first-of-type { border-top: 0; }
footer { color: var(--dim); font-size: 12px; text-align: center; padding: 6px 0 2px; }
@media (min-width: 720px) { body { padding: 20px; } .grid { column-gap: 10px; } }
</style>
</head>
<body>
<header>
  <h1>Swarm</h1>
  <div class="sub" id="stamp">loading&hellip;</div>
</header>
<section class="strip" id="strip"><div class="row" id="strip-row"></div></section>
<main id="bots"></main>
<section class="owed" id="owed" hidden><h2>Owed</h2><div id="owed-rows"></div></section>
<footer>read-only &middot; refreshes every <span id="every"></span>s</footer>
<script>
const STATE = "__STATE_PATH__";
const EVERY = __EVERY_MS__;
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const num = (value) => (value === null || value === undefined) ? "?" : String(value);
const compact = (value) => {
  if (value === null || value === undefined) return "?";
  if (value >= 1e6) return (value / 1e6).toFixed(1) + "M";
  if (value >= 1e3) return (value / 1e3).toFixed(1) + "k";
  return String(Math.round(value));
};
const ago = (seconds) => {
  if (seconds === null || seconds === undefined) return "?";
  const s = Math.max(0, seconds);
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  if (s < 172800) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
};
const money = (value) => (value === null || value === undefined)
  ? "?" : "$" + Number(value).toFixed(2);

function card(bot) {
  const box = el("article", "card");
  const top = el("div", "top");
  top.append(el("span", "alias", bot.alias));
  top.append(el("span", "badge " + bot.badge, bot.badge));
  if (bot.role) top.append(el("span", "sub", bot.role));
  box.append(top);
  if (bot.state === "working" || bot.state === "stuck") {
    const turn = bot.turn_seconds === null ? "?" : ago(bot.turn_seconds);
    const cap = bot.turn_timeout === null ? "" : " / " + ago(bot.turn_timeout);
    box.append(el("div", "doing",
      "working on " + (bot.topic || "?") + " for " + turn + cap));
  } else {
    box.append(el("div", "doing", bot.badge + " for " + ago(bot.since)));
  }
  const meta = el("div", "meta");
  const model = bot.model ? (bot.model + (bot.effort ? "/" + bot.effort : "")) : "?";
  meta.append(el("span", "", "runtime " + model));
  if (bot.rev) meta.append(el("span", "", "rev " + bot.rev));
  meta.append(el("span", "", "pulse " + ago(bot.pulse_age) + " ago"));
  meta.append(el("span", "", "today " + num(bot.turns) + " turns"));
  meta.append(el("span", "", compact(bot.tokens_in) + "/" + compact(bot.tokens_out) + " tok"));
  meta.append(el("span", "", money(bot.cost)));
  if (bot.queue) meta.append(el("span", "", "queue " + bot.queue));
  box.append(meta);
  return box;
}

function render(state) {
  document.getElementById("stamp").textContent =
    "refreshed " + state.refreshed +
    (state.coordinator ? " · coordinator " + state.coordinator : " · no coordinator");
  const row = document.getElementById("strip-row");
  row.replaceChildren();
  if (state.lease && state.lease.term) row.append(el("span", "", "term " + state.lease.term));
  if (state.lease && state.lease.expires_in !== null)
    row.append(el("span", "", "lease " + ago(state.lease.expires_in) + " left"));
  if (state.round) {
    row.append(el("span", "", "round " + (state.round.name || "?") + ": " +
      state.round.done + "/" + state.round.total + " done"));
  } else {
    row.append(el("span", "", "no round in flight"));
  }
  const total = state.total || {};
  row.append(el("span", "", total.bots + " bots"));
  row.append(el("span", "", num(total.turns) + " turns today"));
  row.append(el("span", "", compact(total.tokens_in) + "/" + compact(total.tokens_out) + " tok"));
  row.append(el("span", "", money(total.cost)));

  const bots = document.getElementById("bots");
  bots.replaceChildren();
  (state.bots || []).forEach((bot) => bots.append(card(bot)));

  const owed = document.getElementById("owed");
  const rows = document.getElementById("owed-rows");
  rows.replaceChildren();
  if ((state.owed || []).length) {
    owed.hidden = false;
    state.owed.forEach((debt) => {
      const line = el("div", "line");
      line.textContent = (debt.debtor || "?") + " owes " + (debt.owes || "?") +
        (debt.item ? " on " + debt.item : "") +
        (debt.waiting_on ? " (waiting: " + debt.waiting_on + ")" : "") +
        " · open " + ago(debt.open_seconds) +
        (debt.stalls ? " · stall " + debt.stalls : "");
      rows.append(line);
    });
  } else {
    owed.hidden = true;
  }
}

// Opened as a Telegram Mini App, the page arrives with initData in its URL hash. It is sent
// back on every poll so the server can check it; opened locally there is none, and none is
// asked for. Nothing here loads Telegram's script: the page stays self-contained (#242).
const INIT = (() => {
  const match = location.hash.match(/tgWebAppData=([^&]*)/);
  return match ? decodeURIComponent(match[1]) : "";
})();

async function tick() {
  try {
    const headers = INIT ? {"X-Telegram-Init-Data": INIT} : {};
    const response = await fetch(STATE, {cache: "no-store", headers});
    if (!response.ok) throw new Error("status " + response.status);
    render(await response.json());
  } catch (err) {
    document.getElementById("stamp").textContent = "dashboard unavailable: " + err.message;
  }
}

document.getElementById("every").textContent = String(EVERY / 1000);
tick();
setInterval(tick, EVERY);
</script>
</body>
</html>
"""


# ------------------------------------------------------------------------------------
# The server
# ------------------------------------------------------------------------------------


class DashboardServer:
    """A read-only HTTP server for the page, on the loopback, for as long as it is started.

    ``provider`` is called once per request and returns the JSON-able state; anything it
    raises is a 503 with a JSON body, never a traceback on the wire. The server owns one
    daemon thread, so a stuck request cannot hold the transport's shutdown.
    """

    def __init__(self, provider: Callable[[], Mapping[str, Any]], *, port: int = 0,
                 host: str = HOST, title: str = "Swarm", every: float = 3.0,
                 public_host: str = "", bot_token: str = "", admin_id: Any = 0,
                 init_max_age: float = INIT_MAX_AGE) -> None:
        self.provider = provider
        self.host = host
        self.requested_port = int(port)
        self.title = title
        self.every = every
        #: The name the page is reached under behind a tunnel (#240 slice 2). Empty means the
        #: server only answers for the loopback, and every request is local.
        self.public_host = str(public_host or "").strip().lower()
        #: The Mini App's ``initData`` is checked against these. Empty token or admin means
        #: "no public host", which never asks for one.
        self.bot_token = str(bot_token or "")
        self.admin_id = admin_id
        self.init_max_age = float(init_max_age)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._last_refusal: Optional[tuple] = None

    # -- reading it ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        """Whether the server is up. The thread's own liveness counts: a serve thread that
        died would otherwise be reported as "serving" forever (review, #256)."""
        return bool(self._httpd is not None and self._thread is not None
                    and self._thread.is_alive())

    @property
    def requires_init(self) -> bool:
        """A page reachable under a public name is the admin's alone: it asks for ``initData``."""
        return bool(self.public_host)

    @property
    def hosts(self) -> List[str]:
        return allowed_hosts(public_host=self.public_host)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1]) if self._httpd is not None else 0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{PAGE_PATH}"

    def state(self) -> Dict[str, Any]:
        """The provider's answer, as a plain dict: what the JSON endpoint serves."""
        return dict(self.provider() or {})

    # -- running it ------------------------------------------------------------------
    def start(self) -> str:
        """Bind the port and serve in a background thread. Returns the page's URL."""
        if self._httpd is not None:
            return self.url
        httpd = ThreadingHTTPServer((self.host, self.requested_port), self._handler())
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="swarm-web-dashboard",
                                        daemon=True)
        self._thread.start()
        return self.url

    def stop(self, *, timeout: float = 0.5) -> None:
        """Close the port. Bounded, because this can run on the event loop (#256 review).

        ``shutdown()`` waits for the serve loop to notice, and the join waits for the thread;
        both are capped so a failover or a shutdown cannot stall the transport behind a
        request that is mid-flight. A thread that outlives the timeout is a daemon and dies
        with the process.
        """
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def __enter__(self) -> "DashboardServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- the protocol ----------------------------------------------------------------
    def _handler(self) -> type:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "swarmly-dashboard"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:   # never on stderr
                return

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # The page is self-contained, so nothing but itself may be loaded.
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'; connect-src 'self'")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _not_found(self) -> None:
                self._send(404, b'{"error":"not found"}', "application/json")

            def _host_ok(self) -> bool:
                """Whether the request's ``Host`` is one this server answers for (#256 review).

                A page on a name that resolves to ``127.0.0.1`` is *same-origin* with this one,
                so it could read ``/state.json``; the bind address cannot tell them apart, and
                only the ``Host`` header can. Anything else is a 404 -- the same answer an
                unknown path gets, so the page's existence is not confirmed either.
                """
                given = str(self.headers.get("Host", "") or "").strip().lower()
                name = given.rsplit(":", 1)[0] if given.count(":") == 1 else given
                if server.requires_init and given in server.hosts:
                    return True
                return name in server.hosts or given in server.hosts

            def _init_ok(self, query: str) -> bool:
                """Whether this request carries a valid Mini App ``initData`` for the admin."""
                if not server.requires_init:
                    return True
                raw = str(self.headers.get(INIT_HEADER, "") or "")
                if not raw:
                    for key, value in parse_qsl(query, keep_blank_values=True):
                        if key == "init":
                            raw = value
                ok, why = verify_init_data(raw, bot_token=server.bot_token,
                                           admin_id=server.admin_id,
                                           now=time.time(), max_age=server.init_max_age)
                if not ok:
                    logger.info("web dashboard: refused a request from %s (%s)",
                                self.headers.get("Host", "?"), why)
                return ok

            def do_GET(self) -> None:                       # noqa: N802 - http.server's name
                path, _, query = self.path.partition("?")
                if not self._host_ok():
                    self._not_found()
                    return
                if path in (PAGE_PATH, "/index.html"):
                    body = page_html(title=server.title, every=server.every)
                    self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if path == STATE_PATH:
                    if not self._init_ok(query):
                        self._send(403, b'{"error":"not the admin"}', "application/json")
                        return
                    try:
                        payload = server.state()
                    except Exception:                       # noqa: BLE001 - never on the wire
                        self._send(503, b'{"error":"unavailable"}', "application/json")
                        return
                    body = json.dumps(payload, sort_keys=True, default=str)
                    self._send(200, body.encode("utf-8"), "application/json")
                    return
                self._not_found()

            def do_HEAD(self) -> None:                      # noqa: N802
                self.do_GET()

            def _read_only(self) -> None:
                self._send(405, b'{"error":"read-only"}', "application/json")

            do_POST = do_PUT = do_PATCH = do_DELETE = _read_only      # noqa: N815

        return Handler
