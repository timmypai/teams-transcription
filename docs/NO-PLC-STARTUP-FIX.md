# Preserve the first decoded no-PLC block

The two application receiver paths now select the versioned initializer in
b_no_plc_startup.py: the GPT incoming peer (b_media_profile.py) and the owned Teams
sender/relay target (b_browser_tx.py). The legacy receiver literal and historical
candidate manifests remain unchanged for provenance and before/after comparisons.

Before the first real decoded write, the initializer supplies one separately
identified zero buffer using that AudioData's actual format, then observes two
render quanta on the existing meter under a500ms total initialization bound.
Real timestamps, frame/energy/packet counters and source-quiet evidence are
unchanged. There is no app PCM queue, PLC change or answer-text manipulation.
Close/track end/timeout cancels the initializer and clears its timer/waiter; the
primer closes in finally. The checkpoint is observed render-clock advancement,
not a browser API guaranteeing sink readiness on every version/platform.

Hosted no-model ARM runs41140691906 and41142351873 use the identical core receiver
JS before their documented common QA instrumentation. The complete instrumented
page script differs from application integration; that distinction is retained. Each has three legacy controls missing the first960frames and three
fixed trials retaining all960000frames without gaps/duplication/reordering. Run6
also verifies stop-before-first, stop-during-checkpoint, timeout and protected graph
mutation with enum-specific failure causes and scoped browser cleanup. These are
Chromium156/c409-image synthetic results, not Oracle/Teams remote audibility.
Mono/variable first-block sizes have controlled Node tests, not all-browser coverage.

Residual internal-zero attribution is deferred in issue20; historical Native/RPC
reaped=false remains issue21. Neither is marked passed or resolved by this change.

The normal offline gate includes initializer tests, and existing browser fixtures
select this same current entrypoint. Historical observer/base pins must reject a
new app that no longer matches them. A separately versioned startup runtime/QA
manifest is required for the next cloud QA release; never overwrite old receipts
or make old c409 manifests silently accept this app. Release build/digest/host
payload and specific PR merge/deploy approval are separate from this source work.
