# Examples

## `minimal-bus-demo.sh` — two agents, one bus, no tokens

Runnable right now:

```bash
examples/minimal-bus-demo.sh
```

What it does: clones this repo to a temp dir (the clone is the bus), posts a typed message from
`alice` to `bob` via the real CLI (`scripts/a2a_bus.py send ...`, which writes the envelope file; it commits
and pushes only with `--push`), then prints the envelope file and the inbox view. Expected shape of the output:

- `Message msg-<UTC>-<hash> posted to: <demo>/bus/.agents/inbox/msg-..._alice_pr_review_request.md`
- a YAML frontmatter block (`protocol: a2a/v1.0`, sender/recipient, `intent: PR_REVIEW_REQUEST`,
  `signature: hmac-sha256:...`) followed by the rendered body;
- the inbox listing, where alice's message shows `Valid signature`: the demo signs with a
  throwaway key it puts in `A2A_KEY_ALICE`. Nothing is signed without a key; real agents get
  theirs from `swarmly-keygen --agent <id> --env-file <file> --registry <registry>`, which keeps the
  private key in that `.env` and registers only the public key.

Add your own agent by giving it a `config.yaml` (see `docs/GETTING-STARTED.md`) and a scheduled
poll: `python -m swarmly.telegram.cli --config config.yaml bus-poll --once`.

## `folder-bus-demo.sh`: the same bus without git

```bash
examples/folder-bus-demo.sh
```

A temp directory stands in for a folder that Google Drive for desktop, Dropbox, OneDrive or
Syncthing keeps in sync. `alice` writes an envelope into it with
`scripts/a2a_bus.py --transport folder`, and `bob` reads the inbox from the same folder.
[`docs/TRANSPORTS.md`](../docs/TRANSPORTS.md) shows how agents poll it (`bus.transport: folder`).
