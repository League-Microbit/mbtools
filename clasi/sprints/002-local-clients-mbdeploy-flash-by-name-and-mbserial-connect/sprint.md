---
id: '002'
title: 'Local clients: mbdeploy flash-by-name and mbserial connect'
status: roadmap
branch: sprint/002-local-clients-mbdeploy-flash-by-name-and-mbserial-connect
use-cases: []
issues:
- mbdeploy-flash-by-name-via-mbregistry.md
- mbserial-raw-serial-access-local-or-remote.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 002: Local clients: mbdeploy flash-by-name and mbserial connect

## Goals

Build the two client tools that operate purely on **local** devices
through sprint 001's `mbregistry`: `mbdeploy` (flash by name) and
`mbserial` (raw serial connect). Both are rebuilt as thin registry
clients — no `mbdeploy serve` any more.

## Problem

`mbdeploy` currently runs its own server and touches devices directly;
that's exactly the competing-daemon problem this project exists to fix.
With sprint 001's registry in place, `mbdeploy` and the new `mbserial`
can become pure clients: resolve, lock, act, release — never opening a
port or SWD connection to a board the local registry doesn't own.

## Solution

- `mbdeploy-flash-by-name-via-mbregistry.md` (local half): `mbdeploy
  deploy <name> [--hex FILE]` resolves through the local registry, locks
  with kind `flash`, flashes (locally or via the registry's minimal
  flash op — this sprint should record which path it takes), verifies,
  waits for the post-flash re-probe, and reports the new announcement.
  Keeps the hard-won behavior: retry once on transient probe error,
  erase-and-reflash a locked device, explicit blank-board reporting,
  build integration, and the `--force-relay` guard. `mbdeploy list` as a
  thin view over the registry.
- `mbserial-raw-serial-access-local-or-remote.md` (local half): `mbserial
  <name>` gives an interactive terminal or library-usable serial-like
  object for a locally-attached board, locked with kind `serial`, never
  rebooting the board on connect unless `--reset` is given.

## Success Criteria

- `mbdeploy deploy <name>` flashes a local board end-to-end through the
  registry, with retry/erase/blank-board behavior intact.
- `mbdeploy list` renders the same information as `mbregistry list`.
- `mbserial <name>` opens a local board's serial port without rebooting
  it by default, locks it for the session, and releases on exit.
- Both tools fail fast with a clear message (holder kind + PID) when the
  target is already locked.

## Scope

### In Scope

- `mbdeploy-flash-by-name-via-mbregistry.md` (local flash flow only).
- `mbserial-raw-serial-access-local-or-remote.md` (local transport only).

### Out of Scope

- Remote flash and remote serial connect — split to
  `mbdeploy-flash-by-name-remote.md` and
  `mbserial-raw-serial-access-remote.md` (sprint 003), since both need
  the peering network built there.
- `mbrelay` (sprint 004).

## Test Strategy

(Deferred to Phase 2 detail planning — this is a roadmap-only entry.)

## Architecture

(Deferred to Phase 2 detail planning — this is a roadmap-only entry.
Likely sizes as compact-to-substantial: two new client modules
consuming sprint 001's registry API, no new daemon-side subsystem.)

### Architecture Overview

(Deferred to Phase 2 detail planning.)

### Design Rationale

(Deferred to Phase 2 detail planning.)

### Migration Concerns

(Deferred to Phase 2 detail planning.)

## Use Cases

(Deferred to Phase 2 detail planning. Relevant use cases from
`docs/design/usecases.md`: UC-004 (list, local host only), UC-006 (lock),
UC-007 (unlock), UC-008 (flash by name, local), UC-010 (mbserial
connect, local device).)

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|

Tickets execute serially in the order listed.
