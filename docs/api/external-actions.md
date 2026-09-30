# External actions and notifications — HTTP 1.14

An Invocation describes a computation. An external action describes an intended side effect within that Invocation. Neither preparing an action nor reading a notification calls an external system. Actual calls remain the runtime's responsibility.

## Action protocol

The existing task credential must explicitly include `attempt:execute`. These endpoints select the task from its credential:

| Method and task path | Request / result |
|---|---|
| POST `/api/v1/task/actions` | `action_key`, `operation`, `request` → immutable intention, state and version |
| GET `/api/v1/task/actions` | bounded summary page; `limit`, `after` |
| GET `/api/v1/task/actions/{action_id}` | intention and current state |
| GET `/api/v1/task/actions/{action_id}/events` | immutable event page; `limit`, `after` version |
| POST `.../{action_id}/begin` | `expected_version` → execution, fence, provider key, `can_send` |
| POST `.../{action_id}/report` | `execution`, `fence_id`, `request_key`, `outcome`, `details` → immutable receipt |
| POST `.../{action_id}/abandon` | `expected_version`; only an unstarted intention |

`action_key` is stable for the same logical intention across Invocation attempts. The same key and request return the original action; changed content conflicts. Generated `action_id` values have the form `act_<32 hex digits>`. Version and execution counters are distinct from Invocation attempts.

1. Prepare the intention, then begin its current version.
2. Send to the external provider **only when this response has `can_send: true`**. Concurrent or repeated begins return `false`. Pass `provider_key` to providers that support idempotency.
3. Report `applied`, `not_applied`, or `outcome_unknown`. Duplicate `request_key` + identical observation returns the original receipt. A changed observation uses a new key and cannot overwrite an already resolved effect.
4. On response loss, inspect the action/provider rather than resending. A lost begin response also requires reconciliation; the platform cannot know whether the caller sent the request.

Cancellation, accepted failure and explicit expired-lease recovery mark running actions `outcome_unknown` in the same transaction. Unknown/running effects block Invocation retry; prepared/running/unknown actions block success. A prepared action can be abandoned. A resolved `not_applied` effect may begin a new execution with a new provider key. An `applied` effect cannot be sent again, including after retrying its Invocation.

An inactive task can read its own action history and report a late observation for its own execution while its credential remains valid. It cannot prepare/begin/abandon. All mutations recheck credential rotation and task state under the Invocation lock. Reads do not renew the lease.

## Reconciliation and provenance

Project clients with `invocations:read` can list `/api/v1/projects/{project_id}/invocations/{invocation_id}/actions` and read `/actions/{action_id}` and `/actions/{action_id}/events`.

POST `/api/v1/projects/{project_id}/actions/{action_id}/reconcile` requires `invocations:write` and `artifacts:read`. Body: `expected_version`, known `outcome`, fixed Artifact `evidence` reference, `reason`. The current state must be unknown; the server verifies the Artifact descriptor and actual bytes. A stale version conflicts; after response loss, read the current event before taking another action.

Runtime observations are marked `executor_claim`; reconciliation is `authorized_reconciliation`. The platform verifies identity, fixed evidence and transaction consistency, **not the truth of the provider claim**. Contradictory observations against an already resolved effect return 409; preserve additional evidence as an Artifact for review. No claim of exactly-once external execution is made.

Action request and report details each allow 64 KiB of JSON. Lists omit request bodies. Action heads and their current events are read in one joined query, so page cost does not grow by issuing additional queries per item; missing or inconsistent head evidence still reports corruption. Lists/events accept at most 100 entries and return at most 256 KiB of serialized items, excluding small array/envelope framing; continue with `next_after`. Detail reads return one action. Specifications, action events and observation receipts have canonical SHA-256 digests; original trace content is unchanged.

## Notification feed

| Method and project-relative path | Scope | Behavior |
|---|---|---|
| GET `/notifications?after=0&limit=100` | `notifications:read` | committed event prefix |
| POST `/notifications/consumers` | `notifications:ack` | `consumer_id`; create/retrieve owned checkpoint at zero |
| GET `/notifications/consumers/{consumer_id}` | `notifications:ack` | read own checkpoint |
| POST `/notifications/consumers/{consumer_id}/ack` | `notifications:ack` | `expected_after`, `through`; explicit monotonic progress |

Events cover requested, claimed, result accepted, cancelled, retry requested and lease expired Invocations, plus external-action transitions. Heartbeats produce no notification. Payloads contain resource identifiers/state/digests, never task bearer tokens or trace bodies. A notification is a wakeup; callers still need the normal project/task authority to inspect or execute its resource.

Sequence allocation and the event share the caller's transaction. The per-project head lock prevents a late commit from appearing behind an acknowledged cursor. Failed transactions create no gaps. Sequence values are exact JSON integers through 2^53−1. A consumer ID is a 1–128 character ASCII slug; ownership is immutable. Repeated acknowledgment of the current position is idempotent; stale competing advancement conflicts.

`next_after` is the last returned sequence even at the head, so polling can resume. `history_scope: recorded_events` means only events recorded after this migration; existing state is not synthetically replayed. No webhook dispatcher, automatic claim, event retention deletion, or historical backfill is introduced here. Per-project sequencing serializes short event commits; partitioning is a later throughput decision.

Acknowledgment means **notification progress reported**, not dataset delivery, training consumption, successful replay, or model quality. Readers never implicitly acknowledge. Training consumption will have its own immutable receipts.
