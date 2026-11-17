# Fixed startup runtime QA adapter

The source adapter adds `semantic-startup-v1-offline` and
`semantic-startup-v1-isolated` to the existing manual `oracle-qa.yml`. It selects
the startup app source prepared at `e82bb30900e90c9559b9c3dcb71e532cad86b72e`.
The same three semantic QA modules are reused; no A observer is imported.

`evidence/semantic-startup-candidate-v1.json` is pinned by SHA-256
`0e1996c09d73144485b26ff178a9b0b99fe54d3ba5b636698dbc460eb3109292`.
Its source commit identifies the existing app/QA source, while this separate
manifest identifies the startup bundle/base contract. Receipts call that
relationship `git-source-and-manifest`; it is not a claim that the new manifest
already existed in the earlier app commit. The loader checks all six physical
runtime files before executing the three fixed candidate modules. Tampering or
mixing historical bytes fails before candidate execution.

The fixed profile now pins the verified main release:

```text
source_sha: c9d54b7f72be1c94318386ce7dab7a61df4396e3
image: ghcr.io/paulpai0412/teams-transcription@sha256:57a433bdc059e462100f139466251e4b9670a46770899f6d3b1c9caf9597f657
build_run_id: 41159965976
build_run_attempt: 1
```

The successful manual ARM64 run and artifact 13026647657 were checked through
the release's `release_gate` against real GitHub metadata and downloaded bytes.
This binds source/run/digest/artifact; it is not a signed attestation or an Oracle
observation. All six runtime source hashes also match this release's Git tree.
Request/runtime metadata cannot replace the pins. An unset profile still emits
`release_not_ready` before locks, child execution or quota access; regression
tests explicitly retain that case. The direct container-entry offline seam only
verifies source bytes, returning `model_started=false, release_ready=false` even
after pinning. Bridge offline also returns `live_ready=false` intentionally;
neither offline result grants Live admission. Live additionally requires the
fresh exact healthy runtime, authority, both locks, ledger and cleanup interlock.

Historical A/B/semantic-v1 constants and manifests remain pinned to c409. Their
routes reject the changed app. New projected receipts are bound to the startup
candidate, manifest, source and release profile; a v1 receipt cannot be relabelled
by selecting a new workflow operation. The existing semantic criterion, current
revision delivery, RX, physical mute and owned cleanup checks stay required.

The new live operation uses experiment `semantic-startup-isolated`: two slots
reserved before any model start, serial sessions, both reservations settled as
one attempt after verified cleanup, and no refund for an unused second slot.
Reruns and uncertain cleanup remain blocked. It reuses the existing controller
lock, global QA lock, authority, checkpoint7, 50-start ledger and interlock. No
authority or ledger is initialized or reset by this adapter.

After normal approval/merge of this adapter PR, host installation would replace only the existing
root-owned bridge/entry/contract and add
`/opt/teams-staging/qa-semantic-startup-v1-candidate/` containing the three semantic
modules and the new manifest. Existing `main` oracle-qa workflow/hook/concurrency
remain the execution path. No new credential, account, port, sudo grant or
personal-computer connection is introduced. Source preparation does not install
those files, authorize an invocation or verify host behavior.

Local adapter validation uses source-only loaders and controlled Docker-boundary
fixtures, not Chromium, a model or Oracle. The separate published image's 346
offline contracts and installed-image smoke checks passed; the seven source
audio browser methods were tested before this build, not rerun in this image.
The seven-file payload and adapter workflow are not installed/merged by this
preparation. Old bundles/authority/ledger/locks/interlock/controller/hook must be
preserved. Existing runner permissions suffice; no hook or security change is
required. After approved installation and merge, run the new offline operation
first. Any subsequent isolated QA reserves two of the existing 50 slots; current
usage remains 10/50 until an actual authorized request.
