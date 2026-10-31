# Exclusive Browser Collector Ownership

**Status:** Superseded by the approved shared-worker design in `2026-10-31-shared-browser-collection-workers.md`; retained as the historical exclusive-ownership decision.
**Date:** 2026-10-10

## Context

The local `Galore主机采集` button opens the isolated Chrome profile, but browser workers on both computers poll the same database queue. The browser-collection claim request currently carries no machine identity, and the backend selects the next pending item without considering which computer requested it. Consequently, launching the Galore collector does not reserve the queue for that computer; whichever worker claims an item first can collect it.

The dashboard run heartbeat is also shared and does not identify the computer that produced it. A running item therefore cannot currently be attributed to a particular worker in the monitor.

## Confirmed Outcome

- Only the explicitly selected computer may claim new browser-collection items.
- Scheduled tasks may be prepared/enqueued, but the daily browser-collection queue stays held and cannot start claiming work automatically.
- The user first selects a computer in the daily browser-collection panel, then separately clicks `开始采集`. Selecting a computer alone never starts collection.
- `立即采集全部` may prepare/enqueue queue work, but does not release the hold or start a worker; starting remains an explicit action in the daily browser-collection panel.
- The user can select `Galore主机` as the collector in the daily browser-collection panel; opening its Chrome profile alone does not select a worker or start collection.
- The second computer may remain connected to the dashboard, but cannot claim work while Galore is selected.
- The user can explicitly switch ownership to the second computer from that computer.
- Ownership does not fall back automatically when the selected computer goes offline. The queue waits until the user explicitly switches collectors.
- Work already claimed by the previous collector is allowed to finish. The new collector waits until those claims finish or their leases expire and are safely returned to the queue.
- Existing queue data and collection history are preserved. This change does not reset, clear, or enqueue work by itself.

## Proposed Design

### Worker Identity and Selection

Each Chrome extension installation receives a stable, non-sensitive worker ID stored in that Chrome profile's extension storage. Separate Chrome profiles and separate computers therefore have distinct identities. A friendly label is stored alongside the ID; the isolated profile is labeled `Galore主机`, and the second computer can be labeled `第二台电脑`.

The backend stores the selected worker in a singleton browser-collection control record. Each daily run stores its own dispatch gate, defaulting to held. A database migration creates the control record with no selected worker and adds the held-by-default run gate, so no extension can claim work until the user chooses a worker and explicitly starts the queue. Selecting a worker records its ID, label, and selection time but leaves the queue held.

The daily browser-collection panel shows registered computers with online/offline state and a selector. The user chooses a computer and then clicks `开始采集`; the start action is accepted only if the selected worker is online. A separate `切换到此电脑采集` action on a worker-enabled dashboard can select that computer, but does not start the queue. Merely opening or reloading a worker dashboard does not take ownership or release the queue; this prevents the other computer from reclaiming work just because its tab refreshes. The `Galore主机采集` launcher may open the isolated profile, but launching it alone must not start queue polling.

The scheduler may create the day's queue at its configured time, but it leaves the queue held. Manual queue-preparation actions, including `立即采集全部` and `重新检查今日任务`, also do not release the hold. A previously started run is not silently restarted by a scheduler tick or page reload; the dashboard must show its current held/running/completed state explicitly. Once a run reaches a terminal state, its dispatch gate closes; adding/retrying work on that completed run requires an explicit start again.

### Claim Enforcement and Fencing

Every browser-collection claim includes the worker ID. The backend compares it with the selected worker before returning a pending item. When a collector is selected, a request from another worker, or a legacy request without an ID, receives no item and cannot advance the queue.

Each successful claim also receives a unique claim token stored with the item. Snapshot, failure, verification, and other item-mutating requests must present the current token. A late response from an old browser cannot overwrite an item after its lease expired and another collector reclaimed it.

The backend records the worker ID and token on the claimed item. Worker heartbeats are associated with the ID; an inactive computer's heartbeat does not make the selected collector appear online. The dashboard reports the selected label and whether it is online, waiting for old claims to drain, or offline.

### Handoff Behavior

Changing the selected worker is atomic. Once the selection changes, the old worker cannot claim additional items. Items it had already claimed may finish normally. The newly selected worker cannot claim while any item remains running under another or unknown worker ID. Once those items finish, the new worker proceeds, but only if the user has explicitly started the queue. If a prior worker disappears, the existing lease timeout returns its item for retry; the claim token fences off any late result from that old attempt. Switching computers while a run is active requires explicit confirmation and is treated as a handoff, not as an implicit restart.

The selected worker remains selected if it disconnects or its Chrome closes. The queue stays pending until a user explicitly activates the other computer. There is no automatic fallback or parallel collection.

### User Interface

- Add an online/offline computer selector to the daily browser-collection status area, with a distinct `开始采集` button.
- Keep `Galore主机采集` as a browser-launch action only; opening the isolated profile cannot bypass the queue hold.
- Add `切换到此电脑采集` to a worker-enabled dashboard so the second computer can be selected deliberately; this action does not start the queue.
- Show the selected collector, queue state (`待启动`, `采集中`, or `已完成`), and handoff state in the daily browser-collection status area.
- Scheduled queue preparation, `立即采集全部`, and `重新检查今日任务` do not start collection. The explicit `开始采集` action is the only action that releases the hold and allows claims for the selected online worker.
- While the queue is held, show prepared/pending task counts and a clear reason if start is unavailable (no computer selected, selected computer offline, or prior worker claims still draining).

### Compatibility and Rollout

The migration is additive: existing runs, items, snapshots, and metrics remain unchanged. Legacy extensions without a worker ID cannot claim while the new queue gate is held. Existing in-flight items with no recorded owner are treated as unknown; they must finish or expire before a selected worker can claim after the user starts the queue. Any compatibility period must not allow old extensions to bypass the held state.

Rollout order:

1. Deploy the backend schema and API with the queue held and no owner selected; legacy extensions without a worker ID cannot claim work.
2. Update and reload the extension on both computers.
3. Activate Galore from its launcher; verify the backend reports Galore as selected and that the second computer cannot claim.
4. Test explicit handoff to the second computer, including a running item and an offline/expired lease.

Backend service restart and extension reload are deployment actions and require separate explicit authorization after implementation and tests. No queue reset is part of rollout.

## Scope and Non-Goals

- Applies to the daily browser-collection queue only.
- Does not change the Dianxiaomi handoff queue or review-collection queue.
- Does not add parallel workers, automatic failover, or automatic CAPTCHA handling.
- Does not change the store-page collection logic or collection schedule.

## Acceptance Criteria

1. A scheduled queue can be prepared while held, and neither the scheduler nor `立即采集全部`, `重新检查今日任务`, page reload, or worker launch can cause a claim.
2. Selecting a computer alone leaves the queue held; only a separate `开始采集` click releases it.
3. Start is rejected when no computer is selected, the selected worker is offline, or old/unknown worker claims have not drained.
4. After start, only the selected worker can claim new daily browser-collection items; the other computer remains blocked after reload or heartbeat.
5. Switching to the second computer requires an explicit action and prevents the previous worker from claiming new work.
6. Already-running old-worker items drain before new-worker claims; expired claims can be retried without accepting stale results.
7. The dashboard shows the selected collector, online/handoff state, and held/running/completed queue state without exposing browser credentials or tokens.
8. Tests cover both worker identities, held-state claim rejection, start prerequisites, legacy requests, concurrency/claim locking, handoff, lease expiry, and stale claim tokens.
9. Existing queue items, metrics, current collector profiles, and non-browser queues are preserved.

## Explicit Failure Policy

If the selected computer goes offline before or during collection, no other worker takes over automatically. Before start, the start action is unavailable; during a run, the queue stops making new claims and waits for an explicit handoff. The user must select an online computer and explicitly start or resume collection. Existing claims follow the drain/lease/fencing rules above.
