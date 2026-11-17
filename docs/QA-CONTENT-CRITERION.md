# Versioned synthetic QA content criterion

User direction2026-10-30: meaning must match; exact quotation is not a gate.
`synthetic_current_fact_semantic_v1` evaluates the complete answer against the
current delivered source. New receipts use schema3 and retain `exact_source_quote`
as a diagnostic only. Existing receipts, run conclusions and fixed A/B candidate
bundles remain unchanged. Host review found the previous B answers had complete
correct bodies with only conversational prefixes; historical strict failures are
not rewritten into successful executions.
The actual23/5-character prefixes were not supplied as local fixture bytes. Host
manual reclassification is not proof that this limited parser accepts those exact
strings; that comparison remains pending a nonsecret synthetic fixture handoff.

The local deterministic evaluator supports complete budget and meeting-time facts,
neutral lead-ins, common budget phrasing, equivalent Arabic/Chinese monetary values,
and punctuation/whitespace variants. Monetary comparison uses exact rational
arithmetic, never rounded floating/Decimal context. Currency markers are preserved;
bare「元」does not authorize adding「新台幣」. It checks every assistant utterance, matching
the current expected source version. Wrong numbers/times, stale/multiple values,
negation, unsupported extra content or unknown source versions cannot pass.
It returns explicit pass/fail/unknown and fixed reasons, never raw content. An
expected substring is insufficient. Unsupported languages/fact forms remain
unknown; this is a bounded fixture criterion, not a general natural-language
semantic oracle. Human evaluation can add a separately versioned criterion.
Input caps apply again after Unicode normalization; monetary tokens above64
characters remain unsupported/unknown. Interpreter integer limits are unchanged.

Physical mute, real same-answer RX, source delivery guards, deadlines, owned cleanup
and quota standards remain mandatory. Local delivery/version evidence is not a
model-consumption ACK. No model is called for judging. The existing synthetic
question audio/prompt stays fixed to avoid mixing a stimulus change into the
criterion experiment.

This is local preparation only. The immutable836ecbe isolated candidate andd5f729f
observer still pin the older QA bytes and public projection. They are deliberately
not edited or relabelled. Before a future release, prepare a new fixed candidate,
manifest and matching public projection that carries criterion/status/version;
the current installed bridge cannot silently use this changed module. No existing
run/artifact/ledger/host configuration is modified by this source branch.

The first320-test aggregate rejected changed qa_probe bytes in older fixed loaders
(3failures/2errors). Those results are retained. Their tests now run in separate
processes with physical, hash-checked c409 QA fixture bytes; there is no hash patch
or module-global swap. The fixture records the original commit/path/SHA. Other
guarded base files are also checked before each child starts. Legacy loader tests
still verify their original candidates, and a negative test proves both old
loaders refuse this semantic source tree. This fixes test-fixture mixing, not the
deployment compatibility fence. New matching runtime candidate/projection still
require a separate reviewed change. A
separate initial offline cleanup-task failure is also retained as unconfirmed;
one targeted new-branch and one baseline replay passed without changing its150ms
cleanup fixture or assertions.

## Separate semantic source pin

`evidence/qa-semantic-candidate-v1.json` pins the two new QA/content source files,
criterion version and receipt schema3. Its reviewed preparation digest is
`9036aaf645faecb5a92bf78b81d23daaccc8535542de92c054ff9f8dd52bd8eb`.
It grants no runtime permission and is explicitly incompatible with old A/B
candidate identities. The offline checker only reads/hashes; it never imports or
executes candidate code:

```bash
PYTHONPATH=. python tools/check_qa_semantic_candidate.py --root . \
  --manifest-sha256 9036aaf645faecb5a92bf78b81d23daaccc8535542de92c054ff9f8dd52bd8eb
```

The digest must come from review, not be recalculated from untrusted source at
deployment time. Any change to these source files needs a new manifest/review pin.
The old deployment entrypoint/contract, both candidate manifests and vendored
candidate files remain byte-for-byte unchanged. No environment variable selects
a fake provider; the fixture helper exists only under tests and never runs a
model or starts a browser.
