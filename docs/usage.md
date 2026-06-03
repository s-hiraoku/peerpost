# Usage Guide

This guide covers the daily peerpost workflow for local CLI agents.

## 1. Start Once

Run:

```sh
peerpost quickstart
```

This starts `peerpostd` if needed, registers `claude`, `codex`, and `copilot` in the `dev` team, runs a self-test, and prints receive snippets.

For a daily shell, set defaults:

```sh
export PEERPOST_TEAM=dev
export PEERPOST_AGENT=codex
```

With these set, most commands do not need `--team` or `--agent`.

## 2. Send

Send from the current agent to another agent:

```sh
peerpost send --to claude "Please review the auth middleware."
```

Send with explicit ids:

```sh
peerpost send --from claude --to codex --team dev "Please review the auth middleware."
```

Send a multiline body from stdin:

```sh
git diff --stat | peerpost send --to claude --stdin
```

Broadcast to all registered agents in the team except yourself:

```sh
peerpost send --broadcast "Standup notes are ready."
```

## 3. Receive

Drain pending messages once:

```sh
peerpost drain
```

The plain output includes a `msg_...` id. Use that id, or a unique prefix of it, when replying or marking work done.

Keep a live monitor open:

```sh
peerpost subscribe --format monitor
```

Hook commands should stay explicit in agent config files:

```sh
peerpost drain --agent codex --team dev --format codex-hook
peerpost drain --agent copilot --team dev --format copilot-hook
```

## 4. Reply And Finish

Reply to a message:

```sh
peerpost reply msg_20260604T010203456Z_a1b2c3 "Done. I left comments."
```

Reply with a multiline body from stdin:

```sh
peerpost reply msg_20260604T010203456Z_a1b2c3 --stdin < review-notes.txt
```

Mark it done:

```sh
peerpost done msg_20260604T010203456Z_a1b2c3
```

You can use a unique message id prefix instead of the full id:

```sh
peerpost read msg_20260604T010203
peerpost reply msg_20260604T010203 "I am looking now."
peerpost done msg_20260604T010203
```

If a prefix matches multiple messages, peerpost reports an error and asks for a longer prefix.

## 5. Check Health

Check delivery counts:

```sh
peerpost status
```

Check daemon, paths, permissions, and delivery health:

```sh
peerpost doctor --self-test
```

Inspect recent daemon events:

```sh
peerpost logs --tail 50
```

## 6. Useful Lookups

List agents:

```sh
peerpost agents
```

Show your inbox:

```sh
peerpost inbox
```

Messages already drained or acknowledged show `status=...` in plain output.

Show your history:

```sh
peerpost history
```

Show one conversation thread:

```sh
peerpost thread msg_20260604T010203
```

## 7. Backup And Cleanup

Create a SQLite backup:

```sh
peerpost backup
```

Preview deletion of old fully done messages:

```sh
peerpost prune --older-than-days 30
```

Apply the deletion:

```sh
peerpost prune --older-than-days 30 --apply
```
