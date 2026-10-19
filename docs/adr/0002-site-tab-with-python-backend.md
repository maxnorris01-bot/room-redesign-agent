# 0002. Site tab backed by a separate Python service

- **Status:** proposed (confirm in the first implementation session)
- **Date:** 2026-10-18

## Context

The Room Redesign Agent must be reachable from the portfolio site as its own tab, like the Satellite Point-of-View Tool. Unlike the satellite tool, whose heavy work is a scheduled pipeline writing static files, this tool needs a live backend: a multi-step agent loop that calls a multimodal model and retrieval per request. Visitors must never see the API key, and the bill must be bounded.

## Options considered

1. **Site tab + Python service on Fly, reached through a thin Vercel function.** Reuses `portfolio-project-template` (mock mode, tracing, step and cost caps, eval runner) and the satellite pattern of a Python repo plus the site repo. The Vercel function keeps the Fly URL, the service credential and rate limiting server-side and same-origin. Con: two deploys, and Fly cold starts count against latency.
2. **Site tab + Vercel TypeScript function only.** Single deploy, matches `api/satellite/summary.ts`. Con: the eval harness and tracing are Python; function time limits are risky for a multi-step agent.
3. **Separate web app.** Isolation, but breaks the one-site portfolio story and doubles the polish surface.
4. **iOS (SwiftUI).** Visitors cannot try it without TestFlight.

## Decision

Option 1, as the working direction. The page lives in `portfolio-site` using the tool-page pattern (data file, page component, route and nav entry). The agent lives in this repo. Details (framework, streaming transport, rate limiting) are decided after the spike, because the spike's latency numbers determine whether streaming progress is required.

## Consequences

- This repo needs an HTTP entry point (not part of the spike).
- Public live upload stays off until the spike numbers, rate limits and a workspace spend limit exist. Mock mode and recorded replays can ship first.
- Revisit if the spike shows Fly cold start or the proxy hop breaks the latency target.
