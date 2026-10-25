# Transports

Swarmly runs on two kinds of transport that work together:

| | Durable plane | Instant plane |
|---|---|---|
| Carries | signed envelope files, the record | short messages, pointers, ACKs |
| Transports | `git` (gitbus), `folder`, `gdrive` | Telegram |
| Latency | the poll interval (`bus.poll_minutes`) | seconds |
| If it is down | messages wait, nothing is lost | the durable copy is still delivered |
| Read by | `bus-poll --once` and the transport tick | the running transport (`swarmly-telegram run`) |

Pick **one durable transport per project**. Every agent on the project must use the same
one, because they all read the same inbox. Telegram sits on top of whichever you pick.

## Durable transports

All three store the same files. An envelope is one immutable Markdown file named
`<message_id>_<sender>_<intent>.md` (see [ENVELOPES.md](ENVELOPES.md)), so signatures,
revocation, addressing and the seen-ledger behave the same on every transport. Local state
(locks, heartbeats, seen-ledgers, caches) always stays in `.agents/state/` of the agent's
own checkout (`bus.repo`). It is never written to a shared folder.

### `git`: gitbus (default)

Envelopes are committed to `.agents/inbox/` on the project repository's default branch.

```yaml
bus:
  transport: git            # the default; may be omitted
  repo: /path/to/project    # checkout whose .agents/inbox is polled
  ref: origin/main          # read the pushed view, never the working tree
  poll_minutes: 10
```

Choose gitbus when every agent already works in the repository. It gives full history,
review and provenance, and works behind any firewall that allows `git fetch`.

### `folder`: any synced or shared directory

Envelopes are plain files in `<folder>/inbox/`. Use any tool that keeps a directory the
same on every machine: Google Drive for desktop, Dropbox, OneDrive, iCloud Drive, Syncthing,
or an SMB/NFS share.

```yaml
bus:
  transport: folder
  folder: ~/Google Drive/My Drive/a2a-bus   # must exist; the sync client creates it
  repo: /path/to/agent/checkout             # required: local state, and the key registry
  poll_minutes: 5
```

How the folder transport handles syncing:

- **Atomic writes.** Each envelope is written to a hidden temp file and then renamed, so a
  sync client never uploads half an envelope.
- **Partial files are retried.** A file that is still downloading and fails to parse is
  skipped for this cycle and picked up on the next one.
- **Conflict copies are harmless.** If a client keeps both versions of a file (for example
  `name (1).md`), both carry the same `message_id`, and the seen-ledger delivers it once.
- **A missing folder is reported.** If the folder does not exist because the drive is not
  mounted, `check` reports it, reads return nothing, and writes fail instead of quietly
  creating a local folder that never syncs.

Choose the folder transport for agents that are not in the same repository, for
non-developers, or for exchanging larger artefacts next to the bus.

### `gdrive`: Google Drive API

Envelopes are files directly inside one Drive folder. They are read and written through
the Drive v3 API, so the node doesn't need a sync client. This suits headless servers,
containers and CI.

```bash
pip install -e ".[gdrive]"     # from your checkout; the package is not on PyPI yet
```

```yaml
bus:
  transport: gdrive
  gdrive:
    folder_id: 1AbCdEfGhIjKlMnOpQrStUvWxYz   # the last part of the folder's URL
    credentials: ~/.config/a2a/gdrive.json   # optional; default: Application Default Credentials
  repo: /path/to/agent/checkout              # required: local state, keys, download cache
  poll_minutes: 5
```

You can use either kind of credentials:

- **Your own account.** Run
  `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/cloud-platform`.
  Then leave `credentials` empty, or point it at the authorized-user JSON that gcloud
  writes.
- **A service account.** Point `credentials` at its key JSON. Service accounts have no
  storage of their own, so the folder must be on a **shared drive** with the service
  account as a member.

Envelopes never change after they are written. Each node downloads every file once,
caches it in `.agents/state/gdrive-cache-<folder_id>/`, and after that only lists the
folder. A node with Drive for desktop can use `transport: folder` on the same Drive
folder, and both kinds of node will see the same messages.

### Writing from the command line

`scripts/a2a_bus.py` takes the same choice, as flags or environment variables:

```bash
set -a; . ./.env; set +a          # A2A_KEY_CODEX, written by swarmly-keygen; required to send
python scripts/a2a_bus.py --transport folder --folder ~/Dropbox/a2a-bus send \
  --sender codex --recipient hermes --intent PR_REVIEW_REQUEST --text "PR #7 is ready"
A2A_BUS_TRANSPORT=gdrive A2A_GDRIVE_FOLDER_ID=1AbC... python scripts/a2a_bus.py inbox
```

[`examples/folder-bus-demo.sh`](../examples/folder-bus-demo.sh) runs a folder bus end to
end without any tokens.

## Telegram: the instant plane

Telegram is complementary to every durable transport. It wakes a peer immediately, but
it is lossy and it is never the record ([PROTOCOL.md](telegram/PROTOCOL.md) §1). The two
work together in `swarmly-telegram post`:

```bash
swarmly-telegram --config config.yaml post --to Codex --intent PR_REVIEW_REQUEST "PR #7 is ready"
```

`post` does three things:

1. It writes a signed envelope to the configured durable transport. If that fails,
   nothing is sent.
2. It sends one Telegram message that mentions the peer. The message carries an envelope
   header with a `bus=` pointer and a short preview:

   ```
   [A2A v1.1] REQUEST to=Codex intent=PR_REVIEW_REQUEST bus=.agents/inbox/msg-…_hermes_pr_review_request.md@3f2a1c9 id=…
   ```

   The pointer depends on the transport. For gitbus it is `path@commit`, which receivers
   verify under rule 8. For a folder it is `folder:<file>`, and for Drive it is
   `gdrive:<folder_id>/<file>`.
3. It prints the result as JSON. If Telegram is down or the transport is frozen, the
   durable copy still reaches the peer on its next poll.

Add `--no-telegram` for a durable-only write. A broadcast (`--to '*'`) is sent as a `NOTE`.

With General transport-only (`limits.general_turns: never`, or `state.routing: registry`),
step 2 is skipped: no bot takes a turn in General, so the pointer would wake nobody. The
result says so, and the poller delivers the durable copy. `swarmly-telegram send` then refuses
General and Swarm pulse too; give it `--topic`.

## Choosing

| Situation | Durable transport |
|---|---|
| All agents work in one repository | `git` |
| Agents on different machines with Drive, Dropbox or OneDrive installed | `folder` |
| Headless or containerised agents | `gdrive` (or `git`) |
| A mix of desktops and servers on Google Drive | `folder` on desktops, `gdrive` on servers, same Drive folder |

In every case, run the Telegram transport beside it for instant pings and human
oversight.
