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

## System-wide overview

The repo-wide system overview lives at the root [`ARCHITECTURE.md`](../../ARCHITECTURE.md) — the
bird's-eye view, the six functional layers, the trust-tier model, and the safety invariants. It
stays at the root as the single system-wide entry point; this directory holds the cross-system
constraints and Architecture Decision Records that sit beneath it.

## Decisions

Architecture Decision Records live in [decisions/](decisions/README.md). Only ADRs with an
`Accepted` status are current guidance.
