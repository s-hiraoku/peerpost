# peerpost User Guide

peerpost is a local-first message bus for CLI agents on the same machine.

Use this guide to install peerpost, start the local daemon, register common agents, send and receive messages, and keep the local store healthy.

## Start Here

```sh
python -m pip install -e .
peerpost quickstart
```

For most daily use, set shell defaults:

```sh
export PEERPOST_TEAM=dev
export PEERPOST_AGENT=codex
```

Then the common workflow is:

```sh
peerpost send --to claude "Please review this change."
peerpost drain
peerpost reply <message-id> "Done."
peerpost done <message-id>
peerpost doctor --self-test
```

## Guides

- [Daily Usage](usage.md)
- [Agent Adapter Setup](adapters.md)
- [Release Checklist](release.md)

## Safety Model

peerpost does not send keystrokes, control terminals, execute message bodies, or contact external network services. Message bodies are untrusted peer content.

Hook output includes a safety preamble and avoids repeated stop-hook blocking when detectable.

