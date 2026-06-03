# Usage Guide

This guide covers the daily peerpost workflow for local CLI agents.

## 1. Start Once

Run:

```sh
peerpost quickstart
```

This starts `peerpostd` if needed, registers `claude`, `codex`, and `copilot` in the `dev` team, runs a self-test, and prints receive snippets.

To start the daemon automatically when this PC logs in:

```sh
peerpost daemon install-autostart
```

peerpost writes a launchd user agent on macOS or a systemd user service on Linux, then prints the one OS command needed to enable it.

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

If the sender or direct recipient id is not registered, `send` stores the message and prints a warning.

Send a multiline body from stdin:

```sh
git diff --stat | peerpost send --to claude --stdin
```

Broadcast to all registered agents in the team except yourself:

```sh
peerpost send --broadcast "Standup notes are ready."
```

Broadcast fails if no other agent is registered in the team.
Direct sends to yourself are rejected because they would not create a delivery row.

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
If an `ack` or `done` id does not resolve for the current agent/team, peerpost exits with an error instead of silently updating 0 rows.

## 5. Check Health

Check delivery counts:

```sh
peerpost status
```

`status` shows delivery counts and each agent's most recent pending message time.

Check daemon, paths, permissions, and delivery health:

```sh
peerpost doctor --self-test
```

`doctor` also reports whether the daemon autostart file is missing, current, or stale.
It reports stale pid and socket files, and `doctor --fix` can remove them when the daemon is not running.
It also warns if `PEERPOST_SOCKET` is too long for a Unix domain socket; use a short `/tmp/peerpost-...sock` path.
It reports log file permissions; `doctor --fix` restores `peerpost.log` to private mode.
It runs SQLite `quick_check` and `foreign_key_check` and reports database corruption or referential integrity problems separately from permission issues.
With `--team`, it also reports whether that team has registered agents.
It also reports unregistered sender and recipient ids seen in team messages.
It also reports invalid historical priority values if an old or manually edited database contains them.
It also reports invalid delivery status values if a database was manually edited or corrupted.
It also reports messages without delivery rows if an old or manually edited database contains them.
It also reports invalid metadata JSON; reads fall back to empty metadata instead of failing.

Inspect recent daemon events:

```sh
peerpost logs --tail 50
```

`logs --tail` must be zero or greater.

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

`backup` verifies the snapshot with SQLite `quick_check` and `foreign_key_check` before reporting success. If verification fails, it exits nonzero.

Preview deletion of old fully done messages:

```sh
peerpost prune --older-than-days 30
```

Apply the deletion:

```sh
peerpost prune --older-than-days 30 --apply
```

Use `--backup-first` with `--apply` to create and verify a backup before deleting. If the backup verification fails, `prune` exits nonzero and does not delete messages.

`drain --limit` and `prune --limit` must be positive. `prune` also requires a positive age or past `--before` timestamp.
