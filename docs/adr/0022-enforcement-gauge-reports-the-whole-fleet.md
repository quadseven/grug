# ADR-0022 - The enforcement gauge reports the whole fleet, always

## Status

Accepted (2026-08-01). Refines the `grug.enforcement.state` contract from
#460 / #518. Does not change what enforcement means or how it is detected.

## Context

The enforcement-gap monitor latched. On 2026-08-01 it read `Alert` for over
three hours while **zero** repos were in the gap:

```
04:00:00Z  a private fleet repo emits its LAST datapoint (opted out)
07:18:15Z  monitor 308376323 status = Alert
           last_triggered_ts  = 2026-07-28T16:50:23Z   (3.5 days stale)
           repos in gap       = NONE
```

Two independent causes, both of the same shape: **a repo that stops reporting
keeps its last verdict.**

1. **The repo left the denominator.** `poller_handler` hit `continue` for any
   repo with `tpm_enabled=false`, so an opted-out repo vanished from the gauge
   entirely. This is what that repo did, and it is what the measurement
   above actually proves.

2. **The query filtered on the state tag.** `{enforcement_type:none}` meant a
   repo that GAINED enforcement stopped matching and its series disappeared,
   rather than reporting a healthy value. This one is inferred from the query
   shape, not observed - no instance of it is in the record.

Datadog holds a silent multi-alert group in its last state for **24 hours**
by default. `group_retention_duration` cannot shorten that here: it is
documented for APM Trace Analytics, Audit Trail, CI, Error Tracking, Event,
Logs and RUM monitors, **not** `metric alert`.

A third repo class made it worse. `force_disable_enforcement` is documented in
CONTEXT.md as an enforcement opt-out, but the poller only ever checked
`tpm_enabled`. A repo using the documented escape hatch kept being polled,
kept detecting `none`, and pinned the monitor red permanently.

## Decision

**Every repo the installation can see reports a value on every poll cycle.**
Leaving the gauge is never how a repo stops alerting; reporting a healthy
value is.

- Opted-out repos emit `opted_out` (1.0) instead of being skipped. Both
  opt-outs qualify: `tpm_enabled=false` and `force_disable_enforcement`.
- The monitor thresholds on the **value** (`< 0.5`), never on the
  `enforcement_type` tag, so no state transition can make a series vanish.
- `observability.emit_enforcement_metric` owns the value map. Nothing else
  restates it.

`< 0.5` therefore alerts on `none` (0.0) and `error` (-1.0), and stays quiet
for `grug_managed` / `opted_out` (1.0) and `external` (0.5).

## Consequences

**Detection failures now page.** Previously an auth or rate-limit outage
emitted `enforcement_type:error` for every repo and `none` for none of them,
so the gap monitor went *quiet* during exactly the incident that made
enforcement unknowable. That blind spot is closed, at the cost of a
multi-alert burst when detection breaks fleet-wide. That is the correct
trade: "I cannot tell whether these repos are gated" is an incident.

**One residual case remains.** A repo that is deleted, or whose installation
is removed, cannot emit anything - there is no poll to emit from. Its group
still ages out on Datadog's 24h default. This is inherent, not configurable,
and it is bounded: it needs a repo to leave the estate entirely while red.

**The gauge is now a fleet census, not a problem list.** It reports healthy
and deliberately-ungated repos too. That is the point: a metric you can only
see when something is wrong cannot distinguish "fine" from "gone".

## Verification

`min:grug.enforcement.state{env:prod} by {repo}` immediately after the query
change: 16 repos in scope, 14 at 0.5, 2 at 1.0, **none** breaching.
