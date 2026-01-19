# 0001 - Record architecture decisions

Date: 2026-01-18

## Status
Accepted

## Context
This project spans:
- a cloud backend (VPS)
- distributed field nodes (edge)
- provisioning scripts

As the system grows, key decisions (metrics schema, upgrade strategy, contracts) become hard to track via code alone.

## Decision
We will use **Architecture Decision Records (ADRs)** in `docs/adr/`.

## Consequences
- Small overhead: significant decisions should include an ADR PR.
- Better onboarding and reduced repeated debates.
