# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/eeftychiou/swarmly/security/advisories/new)
for this repository. Include what an attacker can do, the steps to reproduce it, and the commit
you tested.

You should get a first response within a week. Fixes land on `main` and are announced in the
release notes, with credit if you want it.

## Scope

In scope:

- signature creation and verification (`swarmly/crypto.py`, `swarmly/bus.py`);
- the durable transports: git, folder and Google Drive (`swarmly/transports/`);
- the `a2a/2` swarm kernel: envelopes, lanes, keys, the governor and the safety guard
  (`swarmly/swarm/`);
- the gateway handshake and revocation (`swarmly/server.py`, `swarmly/client.py`);
- the Telegram transport: admin and peer authorization, message escaping, loop and replay
  protection (`swarmly/telegram/`);
- anything that could make an agent run a prompt or command it was not addressed with.

Out of scope: vulnerabilities in the agent CLIs themselves (Antigravity, Codex, Reasonix, Hermes, Claude
Code) or in Telegram. Report those to their vendors.

## Known limitations

These are known limits of the current design. Reports that only restate them are not needed;
reports of new ways to exploit them are welcome.

- **The default permission level is full.** `adapter.permissions: full`, the default for
  every runtime, removes every check and every sandbox; `standard` still allows interpreters,
  package managers and git. The default Antigravity command skips its permission prompts.
  Only use a private Telegram group and a private gitbus repository: anyone who can post in
  either can run commands as the agent's user.
- **Agents run as the transport's OS user.** Runtimes do not inherit secret-named variables
  (tokens, `A2A_KEY_*`, passwords), but that is a denylist: the CLI's own API key and secrets
  with other names still pass. As the same user an agent can read the transport's `.env` files
  and, on Linux, `/proc/<pid>/environ`. The boundary is the OS: run each agent as its own
  low-privilege user, or in a container or VM.
- **With HMAC, every receiver can sign as every sender,** because each node holds each HMAC
  sender's secret. Prefer Ed25519 for bus senders.
- **Write access to the bus repository is the trust anchor.** Each node trusts the keys and
  revocations in `config/a2a_config.json` of its bus checkout. Anyone who can push to that
  repository can register a key for a new sender, replace an agent's key, or drop a
  revocation, and every node accepts the change once it pulls; their messages then prompt
  your agents like any registered sender's. Give write access only to people you would let
  run prompts on every agent, and protect the registry: branch protection on the bus branch,
  and a CODEOWNERS entry or required review for `config/a2a_config.json`.
- **Revocation needs every node's copy of the registry.** `revocation_list` is read from each
  node's bus checkout, so it takes effect there only once that checkout carries it.
- **Old signatures during a rollout.** With `"accept_v1_signatures": true` in the registry,
  payload-only signatures are accepted, and their unsigned header can be replayed or
  re-addressed. Leave it off except for the hours of a rollout. `bus.allow_unsigned: true`
  turns verification off entirely (demo buses only).
- **The fast-plane gateway is experimental:** plaintext, and the client does not authenticate
  the server. It binds `127.0.0.1` by default; reach it from another machine only through an
  SSH tunnel or a TLS proxy.
- **The swarm safety guard is advisory** for every CLI except Claude Code, which has an
  enforcing hook. Agents that share an OS user with `~/.a2a/keys` can read those keys.

## Handling secrets

- Bot tokens and `A2A_KEY_*` signing keys belong in the environment or an untracked `.env`. Never
  put them in git. On Windows `chmod` does not protect that file: keep it in your user profile
  directory, where other accounts cannot read it.
- No keys ship. The registry (`config/a2a_config.json`) starts with no agents; create each
  agent's key on its own host with
  `swarmly-keygen --agent <id> --env-file <file> --registry <bus checkout>/config/a2a_config.json`.
- Per-agent `config.yaml` files are untracked (`.gitignore` covers `config.yaml` and
  `*.local.yaml`).
- For HMAC (`--type hmac`) the registry names the key's environment variable: the verifying
  value is the secret itself, so only the name may be committed. A config entry that names an environment variable has no key while that
  variable is unset, and sending refuses without `A2A_KEY_<SENDER>` (or, for HMAC, the variable
  the sender's config entry names).
- For Ed25519 the config holds the public key with `key_type: ed25519`, and only `ed25519:`
  signatures verify against it. The private key stays in `A2A_KEY_<AGENT>` on the agent's host.
- If a key or token leaks, rotate it. Add the agent to `revocation_list` until every node has the
  new key.
