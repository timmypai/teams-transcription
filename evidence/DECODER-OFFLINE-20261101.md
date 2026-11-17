# Decoder setup investigation — synthetic, not Oracle acceptance

Base: `edf853257445ef6b1e9d16e5e332fd34b0918460` (PR36). Results:
[JSON](decoder-offline-20261101.json), [33 focused tests](decoder-offline-focused-20261101.txt).
This work was executed on 2026-11-01, not overnight background work.

The actual current receiver creates its worklet from an inline Blob. There is no
module import, fetch, npm dependency or separately installed JS bundle in that
path. Its generated worklet loads in sandboxed Chromium 156.0.8078.47 on amd64:

| Synthetic condition | Actual constructor result |
| --- | --- |
| No document CSP | succeeds |
| `default-src 'none'; script-src 'self' blob:` | succeeds |
| Same CSP without `blob:` | `worklet_module` / `AbortError` / reason unknown |
| Test-local AudioDecoder absent | `browser_api` / `api_unsupported` |
| Blob denied, test-only native meter substitution | succeeds |

Each case intercepts exactly one synthetic document request; all other routes are
aborted. Docker has network none, no credentials/private configuration, a read-only
filesystem, non-root user, existing seccomp and sandbox enabled. Successful
constructors prove context closed, receiver ended and decoder invalid afterward;
failure cases cannot inspect the constructor's private context, so those two
fields remain null. Their browser contexts are still closed. The owned container
was removed after completion.

The substitution exists only in `tools/decoder_native_meter_candidate.js`, selected
only by `tools/decoder_offline_check.py --native-meter-candidate`. It retains the
generator-to-destination path and silent observation branch, replaces module
loading with native observation, and proposes an explicitly different
`native-capture-after-write-v1` checkpoint. **This constructor test never invokes
that checkpoint or reads real captured samples.** Node tests exercise ordering,
silent primer format, counters, timeout and cleanup with mocks. Neither proves
real browser startup, sample identity, continuity, quiet semantics or remote drain.
The prototype is not ready for runtime selection and changes no app defaults.

This excludes a missing external JS bundle, invalid generated worklet syntax or
globally missing APIs on this tested amd64 Chromium. It does not exclude a
different Oracle ARM build/API capability or a policy/Trusted Types restriction in
the actual meeting frame. Chromium packages are not pinned by the Codex npm pin.
Do not weaken CSP, change sandbox, install packages or downgrade Chromium based
on this evidence.

Minimum missing actual-Oracle evidence, already supported by PR36 diagnostics:
the failed target's `target_stage`, `target_exception_type`, fixed reason,
`target_environment` API/secure-context/sample-rate flags and
`target_document_policy` directive flags, linked to image/source and full Chromium
version. PR35's generic `decoder_setup / Error` cannot distinguish those causes.
Root-page HEAD headers are not the actual target frame's policy. No new meeting,
model request or host inspection was performed for this investigation.

Reproduce from the repository (in the same sandboxed local image, no private mounts):

```sh
python tools/decoder_offline_check.py --output /tmp/decoder-result.json --native-meter-candidate
PYTHONPATH=tests:. python -m unittest test_decoder_offline_check test_b_no_plc_startup test_no_plc_setup_failure test_native_output_attach_diagnostics test_b_document_policy -v
```

The JSON records image, architecture, tool/helper/generated receiver hashes,
resource restrictions and individual outcomes. To run inside Docker, archive only
tracked source plus these tools/tests, extract under container tmpfs and use the
image/security settings recorded there. No image build or pull is required.
This does not replace Oracle/Teams RX/TX acceptance; the last historical model
ledger stays 20/50 without any new consumption or refund.
