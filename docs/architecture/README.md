# Architecture

This directory holds cross-system architecture, durable technical constraints, and architectural
decisions. It explains why the system is shaped a certain way when that explanation affects more
than one system.

## What belongs where

- `docs/systems/` — how a specific system currently works.
- `docs/flows/` — important behavior that moves across systems.
- `docs/architecture/` — broader structure, long-lived constraints, and architectural decisions.

Use this directory for system-wide patterns, durable technical constraints, cross-system
boundaries, major integration strategies, persistence and deployment assumptions, architectural
tradeoffs, and Architecture Decision Records.

Do not use it for ordinary implementation notes, feature documentation, or detailed behavior that
belongs in a system or flow document.

## Decisions

Architecture Decision Records live in [decisions/](decisions/README.md). Only ADRs with an
`Accepted` status are current guidance.
