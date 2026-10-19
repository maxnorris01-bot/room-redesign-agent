# 0001. Record architecture decisions

- **Status:** accepted
- **Date:** 2026-09-21

## Context
Design choices in an LLM application (model, retrieval strategy, eval method) are easy to forget and hard to justify later.

## Decision
Record each non-obvious decision as a short ADR in `docs/adr/`, using `0000-template.md`. Keep each to one page.

## Consequences
The README can link to these instead of explaining tradeoffs inline, and reviewers can see the reasoning behind the design.
