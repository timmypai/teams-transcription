# External postmeeting host adapter

`teams_transcription.postmeeting_adapter.process_postmeeting_once(store, state_dir, executable, model, owner)` handles one already durable ready intent in the existing SQLite data root. It is an explicit host entrypoint, not a new daemon, timer, CLI command, provider, or dispatch service. It is not registered in `AssistantDaemon`, and the canceled legacy summary loop remains disabled.

A host that is separately verified and authorized could call this function after a completion wake-up. No such deployed completion wake-up or dot dispatcher has been found in this repository or in the currently exposed tools; this sentence is an integration requirement, not an existing capability. It claims one leased job, reads its immutable source snapshot plus latest valid series state, keeps only that invocation's lease alive, and uses the existing `CodexRPC` app-server. It requires the pinned CLI, inherited ChatGPT account authentication, OpenAI ordinary Codex model, disabled tools/apps/web search, owned thread and turn completion, valid source citations, and successful provider cleanup. It never runs shell/SQL work requested by the model. The application publishes through its fenced store API only after those checks.

The host-created `CompletedPostmeeting` receipt contains the validated result, actual model, thread/turn IDs, and SHA256 of the canonical complete input snapshot. The store must compare that input digest in the publication transaction. Model JSON cannot supply model/provider/completion metadata. Controlled test receipts and synthetic RPC notifications are test evidence only, not real model completion evidence.

Cancellation, expired leases, failed authentication, forbidden tools, mismatched model/provider, foreign citations, absent completed turn, and uncertain cleanup do not publish. Failure codes are fixed strings without private exception text. A future explicit host invocation can reclaim recoverable work; the adapter does not schedule retries. Publication races with cancellation are awaited to their actual SQLite outcome, so a retry can observe an existing publication.

## Remaining host authority boundary

This checkout confirms `codex-cli 0.159.0-alpha.3` is installed. It does not establish that dot has an authorized autonomous event handler for this deployment, a usable same-deployment ChatGPT account, an approved completion-notification destination, or a running host bridge. No actual model/authentication call was performed by the adapter implementation tests. No Page, external notification, OAuth grant/token transfer, Oracle change, deployment, or persistent process was created. Wire this one-shot function only through a separately verified existing host trigger; until then the UI must show the durable intent as waiting for external processing.

## Verification

`PYTHONPATH=tests:. python -m unittest test_postmeeting_adapter -v` exercises the public adapter and synthetic external RPC boundary, including completion binding, cleanup, version/auth rejection, forbidden tools, citation rejection, empty queue, cancellation, and lease loss. It does not prove real Codex or Google execution.


## Concrete entrypoint audit (2026-11-02)

| Entry | Verified contract | Can it currently deliver an automatic postmeeting run? |
| --- | --- | --- |
| `AssistantDaemon._execute` → `AssistantStore.finish_run` | Existing runtime path seals capture and, in this candidate, writes snapshot + ready job atomically. It then clears active capture ownership. | No outbound completion dispatch exists. This is a local commit boundary, not an event delivered to dot. |
| `session_events` | A private SQLite journal written by the app; no remote consumer, subscription or wake-up protocol. | No. Merely adding a journal row cannot wake a cloud agent. |
| `CodexRPC` / `codex app-server --stdio` | Real installed CLI `0.159.0-alpha.3`; initialize/account/read/thread/start/turn/start with bound completed notification and cleanup. Existing inherited deployment ChatGPT auth is required. | It executes when invoked. It does not watch Calendar or SQLite, and CLI availability in this Cloud workspace does not prove the deployment identity works. |
| `process_postmeeting_once` | Complete one-shot library implementation: claim → frozen input → heartbeat → restricted RPC → cleanup → fenced atomic publish. | No trigger is registered. An operator command would be explicit execution, not automatic after-meeting delivery. |
| `serve --local-teams-control` / same-UID Unix socket | Opt-in fixed operations are prepare/view/join/output-disable/stop, with existing authority/request checks. No transcripts or arbitrary commands. | No postmeeting operation, completion push, generic executor or dot callback. Do not repurpose a Teams admission grant as postmeeting dispatch authority. |
| Existing Oracle workflows | Manually dispatched, allowlisted host deployment/QA/diagnostic contracts. | No postmeeting event subscription. This task did not dispatch or modify them, touch Oracle, or expand host allowlists. |
| Exposed ChatGPT apps/tools | No deployment SQLite connector or callable dot/Codex meeting-completion dispatcher was discovered. Page/Library writes and other app webhooks are separate destinations/authorities. | None verified for this event. A timer, GitHub event, Page auto-update, arbitrary webhook, credential transfer or exported private source would introduce a different integration and is not silently substituted. |

### Required external host execution contract

A deployment owner must identify a **real existing host** (principal, installed process/service and entrypoint) before this can be called automatic. The host must already have access to the same private data root and the same authorized persistent ChatGPT `CODEX_HOME`; it must not copy or export tokens. It must be able to launch the pinned installed executable with `/dev/shm` tmpfs and the existing egress restrictions. `auth-preflight` is metadata only; a same-deployment explicit `auth-check` verifies account admission without granting a new login. Neither was run on Oracle by this task.

The completion wake-up must occur only after `finish_run` commits. Its envelope needs only the opaque job key/session binding, never source text, meeting URL or OAuth. Delivery must be durable/at least once, and the host must inspect ready intents on its own existing startup/recovery path so a crash between commit and wake-up is recoverable. A one-time callback alone, without replay, is insufficient. The current adapter claims one ready job; a verified consumer must account for the queue and repeated wake-ups rather than assuming one callback necessarily corresponds to its triggering session.

A consumer should acknowledge the handoff only after the app's transaction confirms publication or an already published/notified intent. Known failures remain ready with a fixed code; lease expiry fences stale writers. Unknown provider cleanup becomes blocked and requires that host owner to establish cleanup rather than retry blindly. There is no total meeting/summary deadline or new periodic scheduler in this contract.

The app's atomic local inbox is already the configured completion-notification destination. Delivering outside the private admin UI requires an explicitly selected destination and corresponding authorization; none is guessed. The adapter uses no new API key, provider or paid fallback. This does not assert that real turns are free: a deployment owner must establish that its existing plan/quota and extra-usage policy meet the approved no-new-cost boundary before real execution; this task made no model call or billing claim. A new autonomous dispatcher/service registration, socket action, outbound endpoint, remote SQLite access, grant/token transfer, deployment change, or external destination is **new authority** and requires separate approval. Ordinary source changes/tests and the local SQLite/RPC seams can be prepared without that approval.

### Verification boundary

`test_postmeeting_host_contract` exercises the actual SQLite store and actual one-shot orchestration together, with only the external RPC replaced by a clearly synthetic boundary fixture. It proves known publication/notification idempotency and blocked unknown cleanup; it is not a real Codex run or proof that any host wakes automatically.
