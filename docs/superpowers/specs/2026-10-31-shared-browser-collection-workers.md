# Shared Browser Collection Workers

**Status:** Approved for implementation
**Date:** 2026-10-31
**Supersedes if approved:** `2026-10-10-exclusive-browser-collector-ownership.md`

## Context

The daily browser collection queue currently has exclusive worker ownership. The claim service only admits the selected worker, blocks claims while a task from another worker is running, and serializes store discovery to one running store item. This prevents two connected collector computers from making progress at the same time.

The extension already sends a stable worker ID with claim and result requests, uses per-item claim tokens, heartbeats active claims, and keeps at most two collection pages open per computer. The backend already uses database row locks while claiming tasks. The requested change is to make the existing daily queue shared among online collector computers while preserving its manual start and safety gates.

## Intended Behavior

- The daily queue remains held until a person selects an online collector computer and explicitly clicks `开始采集` or `继续采集`.
- The selected computer is the start authorization only. Once dispatch is enabled, every registered computer with a fresh heartbeat can claim tasks from the same queue.
- When starting or resuming, unexpired claims owned by other computers do not block dispatch. Those claims remain valid under their worker IDs and tokens while new claims are distributed from the shared queue.
- Any online computer may claim the next eligible task. When only one computer is online, it continues to drain the whole queue.
- Two computers may collect different store tasks at the same time. A task has one worker ID and one claim token, so only that worker can submit its result and a stale result cannot overwrite a later claim.
- Keep the existing extension limit of at most two open collection pages per computer. The control panel reports the number of tasks currently claimed by each computer.
- Selecting a different start computer while a run is active does not pause dispatch, revoke claims, or initiate a handoff. It only changes which online computer must be selected for a future manual start or resume.
- Keep the existing manual pause, platform verification, daily queue creation, monitored-store selection, scheduling, and data retention behavior.

## Claiming and Concurrency

The claim endpoint accepts a worker only when that worker has a heartbeat within the existing two-minute online window and the run's dispatch gate is enabled. It no longer compares the requester with `selected_worker_id` and no longer rejects a claim because another worker has a running item.

Claims remain serialized briefly by the existing run/control row locks and pending-item row lock. Claim transactions assign one pending item, worker ID, and random claim token, then commit. Browser work occurs after that transaction, allowing distinct workers to process distinct stores concurrently.

The store-discovery phase remains active while any store item is pending or running. Product collection can begin only after all store items are terminal, as it does today. Run counts and completion state continue to be refreshed from item states.

No new queue, worker partition, database migration, or extension protocol is expected. The worker response can include a computed `active_claim_count` per registered computer without storing a duplicate counter.

## Failure Recovery and Human Gates

- Preserve the existing five-minute claim lease, heartbeat renewal, maximum task runtime, retry count, and claim-token fencing.
- When a worker stops heartbeating and its claim expires, return that item to the shared queue under the existing retry rules. Any online worker can then claim it.
- Manual pause and AliExpress verification remain queue-wide gates. No worker receives new claims while either gate is active.
- After a person resumes a platform-verification pause, retain the verification task on its original worker while that worker remains online, so the browser session that encountered the challenge can continue. If that worker is offline at resume time, clear its claim and return the task to the shared queue; the next computer may need its own login or verification.
- Store identity review continues to use the existing review flow and does not authorize another worker to bypass identity safeguards.

## Dashboard

- Keep the computer selector and separate `开始采集` / `继续采集` button.
- Explain that selecting a computer authorizes the manual start; all online computers can collect after start.
- Remove the active-run handoff confirmation and old-worker drain state from selector changes.
- Show each online/offline worker and its current claimed-task count. Keep the selected label override behavior intact.
- Keep the global pause, verification, pending, completion, and failure indicators.

## Scope and Non-Goals

- Applies only to the daily browser-collection queue.
- Does not change Dianxiaomi handoff queues, review collection, store-pool reset ownership, or scheduled queue preparation.
- Does not start a queue automatically, reset or replace today's run, alter monitored-store membership, change the collection identity rules, or change the per-computer open-page limit.
- Does not deploy or restart NAS services as part of implementation. Deployment and two-computer live acceptance are separate rollout actions.

## Acceptance Criteria

1. A run remains held until an online computer is selected and a person explicitly starts it.
2. With two online workers and at least two pending store tasks, each worker can claim a different task, and neither receives the same item or claim token as the other.
3. A worker with a stale heartbeat or missing worker ID cannot claim new tasks. Result submissions remain bound to the owning worker ID, current claim token, and unexpired lease.
4. While one worker has a store task running, another online worker can claim a different store task; the run remains in store discovery until every store task is terminal.
5. With only one online worker, that worker can continue claiming distinct tasks until the queue is drained.
6. An expired claim returns to the shared queue according to existing retry limits, and a late response using its former token is rejected.
7. Manual pause and platform verification stop new claims for every worker; resume preserves the challenge task's browser owner when online.
8. The dashboard shows the selected start computer and per-worker claimed-task counts; changing selection during an active run does not pause or revoke work.
9. Existing queue rows, snapshots, metrics, monitored-store membership, and other queues are unchanged.
10. After deployment, a controlled live run with two online computers shows both holding distinct active claims at the same time; this live acceptance is reported separately from automated test results.

## Rollout Notes

Implementation should preserve the current NAS queue state. Before deployment, inspect the running daily run and outstanding claims. After the backend update, confirm both computers report fresh heartbeats; reload an extension only if it fails to advertise its worker ID or heartbeat. Live acceptance should use the held daily queue, inspect the first concurrent claims, and verify both worker IDs and distinct item IDs in the dashboard/API before allowing normal collection to continue.
