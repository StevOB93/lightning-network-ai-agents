# A2A — Agent-to-Agent Communication

Placeholder for future work enabling multiple AI agents to coordinate over the Lightning Network.

In an A2A setup each agent runs on a separate machine with its own Lightning node. Agents can pay each other for services (computation, data, routing) using standard BOLT11 payments, with the payment itself serving as the authorization and proof of work.

## Planned scope

- Agent discovery protocol over Lightning gossip
- Service advertisement via node metadata (alias, color, custom TLV records)
- Request/response framing using keysend or BOLT12 offers
- Integration with the existing pipeline: an agent can route a sub-task to a remote agent and incorporate its response into the final answer

## Status

Not yet implemented. This directory is a placeholder.
