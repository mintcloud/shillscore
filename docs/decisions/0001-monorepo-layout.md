# ADR 0001 — monorepo layout

**Date:** 2026-05-04
**Status:** Accepted

## Context

shillscore is a frontend (dashboard) plus a backend (API + ingest jobs). They share types: account IDs, mention shapes, leaderboard rows, score calculations.

## Decision

Single repo, three top-level workspaces: `apps/web`, `apps/api`, `packages/shared`. Plus `infra/` (docker-compose, SQL migrations) and `scripts/` (ingest + price snapshot jobs) at the root.

## Consequences

- One PR can cover end-to-end changes (e.g. add a field to `Mention`: update shared type, API serializer, web component, all in one diff).
- One CI pipeline. One issue tracker. One README.
- For a portfolio repo, "I built this end-to-end" reads as one repo, not three.
- Cost: slightly more setup than a single-app repo. Worth it given the type-sharing need.

## Alternatives considered

- Three separate repos (web / api / shared-types as a published package). Rejected — overhead too high for a solo project, and the type-sync friction is real.
- Single Next.js app with API routes (no separate `apps/api`). Rejected — ingest jobs need to run as long-lived processes, and Next.js API routes aren't the right shape for that.
