# Isolated Browser Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Isolate local collection from the user's everyday Chrome profile.

**Architecture:** The local extension profile only polls the queue when explicitly marked as the isolated collector profile. The daily task launches Chrome with a unique user-data directory and that marker; LAN workers remain enabled by their existing host URL.

**Tech Stack:** Chrome Extension Manifest V3, PowerShell, Windows Task Scheduler, Node.js tests.

**Spec:** `docs/superpowers/specs/2026-10-10-isolated-browser-collector.md`

## Global Constraints

- Do not start, close, focus, or reload Chrome during implementation.
- Do not restart services, reload extensions, or mutate the queue.
- Keep the dedicated collector profile separate from the existing Dianxiaomi profile.
- Do not break the LAN worker at `192.168.66.107:3000`.

## Review Focus

- A normal local dashboard must not claim collection work, even after a queue button click.
- The isolated local profile must enable collection from its explicit URL marker.
- The existing LAN worker URL must continue to enable collection.
- The launcher must not use a global Chrome kill or the user's default profile.

---

### Task 1: Gate extension worker mode by profile

**Files:**
- Modify: `extension/background.js`
- Modify: `extension/dashboard_bridge.js`
- Test: `tests/extension_background_worker.test.cjs`

- [x] Add failing tests showing that a normal local dashboard cannot enable polling, the isolated local profile can, and the LAN worker still can.
- [x] Run those tests and confirm they fail for the current worker-mode behavior.
- [x] Make dashboard queue events poll only when the profile was already enabled as a worker; derive local eligibility from the isolated profile marker.
- [x] Run the targeted tests and the complete extension Node test suite.

### Task 2: Launch the dedicated local Chrome profile

**Files:**
- Create: `scripts/open-browser-collector-chrome.ps1`
- Modify: `start-browser-collector.bat`
- Modify: `scripts/install-browser-collector-task.ps1`
- Modify: `start.bat`
- Test: PowerShell parser validation and launcher inspection

- [x] Add the dedicated-profile launcher with a separate user-data directory and isolated worker URL.
- [x] Make the scheduled launcher start or reuse the dedicated collector profile, while the everyday dashboard opens without worker mode.
- [x] Validate PowerShell syntax, task action, profile path, and URLs without starting Chrome.

### Task 3: Update operator instructions

**Files:**
- Modify: `README.md`
- Modify: `extension/README.md`

- [x] Document the dedicated profile's one-time sign-in/verification and clarify that implementation does not reload or deploy it.
- [x] Re-read the spec and verify every requirement against code, tests, and documentation.
