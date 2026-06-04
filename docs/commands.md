# Command Reference

This page is a compact map of the peerpost CLI. Start with `quickstart`, then use `send`, `drain`, `reply`, and `done` for daily agent-to-agent coordination.

## Daily Flow

```sh
peerpost quickstart
peerpost send --to claude "Please review this change."
peerpost drain
peerpost reply <message-id> "Done."
peerpost done <message-id>
```

Set defaults in a shell to keep commands short:

```sh
export PEERPOST_TEAM=dev
export PEERPOST_AGENT=codex
```

## Setup And Daemon

| Command | Purpose |
| --- | --- |
| `peerpost quickstart` | Start the daemon if needed, register common agents, run a self-test, and print receive snippets. |
| `peerpost setup` | Print adapter snippets and optionally register selected agents. |
| `peerpost daemon start` | Start `peerpostd` in the background. |
| `peerpost daemon start --foreground` | Run `peerpostd` in the current terminal. |
| `peerpost daemon status` | Check whether the daemon responds. |
| `peerpost daemon stop` | Ask the daemon to shut down cleanly. |
| `peerpost daemon install-autostart` | Write a launchd or systemd user autostart file. |
| `peerpost daemon uninstall-autostart` | Remove the peerpost autostart file. |
| `peerpost paths` | Show the active home, database, socket, pid, and log paths. |

## Agents

| Command | Purpose |
| --- | --- |
| `peerpost join --agent <id> --type <type>` | Register or update a local agent in a team. |
| `peerpost agents` | List registered agents. |
| `peerpost leave --agent <id>` | Remove an agent registration. Existing messages remain in history. |
| `peerpost status` | Show delivery counts and each agent's most recent pending message time. |

Supported agent types include `claude-code`, `codex`, `copilot`, `antigravity`, and `generic`. Unknown types are allowed with a warning.

## Messages

| Command | Purpose |
| --- | --- |
| `peerpost send --to <agent> "message"` | Send a message from the current agent to one recipient. |
| `peerpost send --broadcast "message"` | Send to every registered agent in the team except the sender. |
| `peerpost drain` | Print pending messages for the current agent and mark them delivered. |
| `peerpost subscribe --format monitor` | Keep the socket open and print new messages as they arrive. |
| `peerpost inbox` | Show non-done messages for the current agent. |
| `peerpost read <message-id>` | Show one message and its delivery state. |
| `peerpost reply <message-id> "message"` | Reply to a message. |
| `peerpost ack <message-id>...` | Mark messages acknowledged. |
| `peerpost done <message-id>...` | Mark messages done. |
| `peerpost history` | Show team message history. |
| `peerpost thread <message-id>` | Show one conversation thread. |

`read`, `reply`, `ack`, `done`, and `thread` accept a unique message id prefix.

## Hooks And Snippets

| Command | Purpose |
| --- | --- |
| `peerpost drain --format codex-hook` | Codex Stop hook output. Prints only JSON. |
| `peerpost drain --format copilot-hook` | Copilot `agentStop` hook output. Prints only JSON. |
| `peerpost install-snippets --adapter all` | Print copy/paste adapter snippets. |
| `peerpost snippets --adapter all` | Alias for `install-snippets`. |

Hook output starts with the safety preamble and avoids repeated stop-hook blocking when detectable.

## Health And Maintenance

| Command | Purpose |
| --- | --- |
| `peerpost doctor` | Check paths, permissions, daemon state, version, socket, logs, and database integrity. |
| `peerpost doctor --team dev --self-test` | Also check team delivery health and run a temporary message-flow self-test. |
| `peerpost doctor --fix` | Repair safe local filesystem issues such as permissions and stale pid/socket files. |
| `peerpost logs --tail 50` | Print recent daemon log lines. |
| `peerpost backup` | Create and verify a SQLite backup. |
| `peerpost prune --older-than-days 30` | Preview deletion of old messages whose deliveries are all done. |
| `peerpost prune --older-than-days 30 --apply --backup-first` | Back up, verify the backup, then delete matched done messages. |

Most script-facing commands support `--format json`.
