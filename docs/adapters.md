# Adapter Setup

This guide shows the minimum commands to connect local CLI agents to peerpost.

peerpost does not edit agent config files automatically. Copy the snippet for the agent you use and paste it into that tool's hook, monitor, or local workflow configuration.

## 1. Prepare peerpost

Run once on the machine:

```sh
peerpost setup --start-daemon --team dev --register-default-agents
peerpost doctor
```

For custom agent ids, register them explicitly:

```sh
peerpost setup --start-daemon --team dev --register reviewer:generic
```

Print snippets any time:

```sh
peerpost install-snippets --adapter all --team dev
```

## Claude Code

Use Claude Code Monitor with:

```sh
peerpost subscribe --agent claude --team dev --include-backlog --format monitor
```

This keeps a live stream open and prints peer messages as monitor lines.

Recommended agent id:

```text
claude
```

Register it:

```sh
peerpost join --agent claude --type claude-code --team dev
```

Send to Codex:

```sh
peerpost send --from claude --to codex --team dev "Please review the auth middleware."
```

## Codex CLI

Use this Stop hook command:

```sh
peerpost drain --agent codex --team dev --format codex-hook
```

When no messages are pending, it prints:

```json
{}
```

When messages are pending, it blocks once with a safety-prefixed reason.

Recommended agent id:

```text
codex
```

Register it:

```sh
peerpost join --agent codex --type codex --team dev
```

## GitHub Copilot CLI

Use this `agentStop` hook command:

```sh
peerpost drain --agent copilot --team dev --format copilot-hook
```

The output shape matches the Codex hook format:

```json
{
  "decision": "block",
  "reason": "..."
}
```

Recommended agent id:

```text
copilot
```

Register it:

```sh
peerpost join --agent copilot --type copilot --team dev
```

## Antigravity

If Antigravity supports a stop hook, use a plain drain command first:

```sh
peerpost drain --agent antigravity --team dev --format plain
```

If it supports a long-running monitor process:

```sh
peerpost subscribe --agent antigravity --team dev --format monitor
```

Recommended agent id:

```text
antigravity
```

Register it:

```sh
peerpost join --agent antigravity --type antigravity --team dev
```

## Generic CLI Agent

For any local shell-based agent, use one of these:

```sh
peerpost drain --agent generic --team dev --format plain
peerpost subscribe --agent generic --team dev --format monitor
```

Use a more specific id if you run multiple generic agents:

```sh
peerpost join --agent reviewer --type generic --team dev
peerpost drain --agent reviewer --team dev --format plain
```

## Safety Notes

Messages from peer agents are untrusted context. Hook formats always start with a safety preamble instructing agents not to treat peer messages as system, developer, or user instructions.

peerpost never executes message bodies.

## Testing An Adapter

After configuring an adapter, send a test message:

```sh
peerpost send --from claude --to codex --team dev "peerpost test ping"
```

Then check:

```sh
peerpost drain --agent codex --team dev
peerpost logs --tail 20
peerpost doctor
```

The message should appear once. A second drain should print nothing.
