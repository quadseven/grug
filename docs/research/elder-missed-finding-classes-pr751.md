# Three finding classes Elder missed on PR #751 (#752)

Part of #707.

PR #751 was a small change touching four files. At its reviewed head
`61dce166be`, a src-a reviewer caught three real defects. Elder published
one finding there, `high-complexity` on the new attester's `main()`, and
that finding came from the deterministic complexity scanner. The LLM arm
contributed nothing on a diff that carried a production regression.

This note records the fixtures, the baseline, and a reachability decision
for each class. The fixtures and their tests live in
`services/webhook/elder_eval/missed_classes.py` and
`services/webhook/tests/test_elder_missed_classes.py`.

## Fixtures

Each fixture is built from the real reviewed head, not the merged diff
(the merged diff already contains the fixes):

- `fixtures/pr751/installations.diff` and
  `fixtures/pr751/attest_persona_surface_coherence.diff` hold the real
  hunks of the two Python files that carry all three defects.
- `fixtures/pr751/installations.py.txt` is the head snapshot of
  `services/api/installations.py`. Production sends a changed file's full
  content when it fits the #336 budget (800 lines, 40k characters), and
  this file, at 626 lines, does. Its non-ASCII punctuation is
  transliterated one-for-one, and a test checks that every hunk line
  still sits at its header's line number in the snapshot.
- The workflow and dashboard hunks are omitted. No defect anchors there.

A catch is line-anchored: the finding must sit on the reference finding's
path, inside its defect line range, and its message must name the defect
(one of the fixture's signal terms). Class-level credit, as in the ledger
replay, would count any `correctness` finding on the PR as a catch. That
is the wrong measure for classes where a false positive costs an
operator's time.

| Fixture | Class | Defect lines | Counterexample, proven in CI |
|---|---|---|---|
| `cross_hop_tpm_default_side_effect` | cross-hop dataflow | installations.py 65-76, 446-475 | the head `RepoConfigPayload(sentinel_enabled=False)` validates to `tpm_enabled=True` |
| `weak_assertion_keyword_only_forwarding` | weak assertion | attester 119-129, 168-182 | `sentinel_enabled=body.guard_enabled`, or a constant, passes the reviewed attester and fails the fixed one |
| `reverse_direction_stale_ui_mapping` | reverse-direction completeness | attester 201-207 | a stale `"deleted_persona": "ghostcard"` entry plus a ghost card passes the reviewed attester and fails the fixed one |

The weak-assertion and reverse-direction proofs run the reviewed attester,
built from the fixture diff, and the shipped attester against the same
minimal synthetic tree. Both pass a coherent tree, so the reviewed
attester's pass on the counterexample shows the guard was fooled, not
that the tree failed to parse.

## Baseline: recall 0/3

`services/webhook/elder_eval/missed_classes_baseline.json` records
production's review of head `61dce166be` on 2026-07-24, including the one
finding it posted. A CI test re-scores that finding against the fixtures,
so the 0/3 is recomputed rather than asserted.

| Measurement | Cross-hop | Weak assertion | Reverse direction | Recall |
|---|---|---|---|---|
| production, 2026-07-24 | missed | missed | missed | 0/3 |

No live replay has been recorded yet. `python -m elder_eval.missed_classes`
replays the fixture evidence through Elder's exact prompt and parser on
each backend configured for the benchmark (see
`sast_benchmark.backends`), and `--record` adds that run to the baseline
file beside the production entry. An errored replay is reported as
errored and never recorded as a zero.

## Reachability decisions

### Cross-hop dataflow: needs a structural change

The defect is three hops: the payload default
(`tpm_enabled: bool = Field(default=True)`, line 65), a read of that
field about 400 lines later (`now_enabled = body.tpm_enabled`, line 471),
and the side effect that read gates (`_toggle_enforcement(...)`, which
creates a branch ruleset). The diff adds none of these lines. Line 65 is
only diff context, and lines 471-475 are in no hunk. What the diff adds
is the new optional fields that make a sparse payload such as
`{"sentinel_enabled": false}` the natural shape. That is what activates
the latent defect.

For this file the evidence did reach the prompt: the full-file block
carries line 471. But it arrives framed as context, under an instruction
to flag only diff additions, so the reasoning Elder would need runs
against its own contract. To find this class, Elder has to anchor a
finding on an added line whose justification lives wholly in unchanged
code. The evidence is also fragile. Above the full-file budget, the block
is dropped and only enclosing signatures are rendered (#1227), so the
read and the side effect would leave the prompt entirely.
`installations.py` is 680 lines today, close to that limit.

A rule line cannot fix either problem. Reaching this class needs two
structural changes. First, evidence: the reads of a field that the diff
declares or changes, within the same function, rendered whatever the
file size. Second, an anchoring contract for "this diff makes a latent
defect in unchanged code reachable". That is a design decision for #707
to make, so no implementation slice is filed for this class here.

### Weak assertion: reachable by a prompt rule

The whole defect sits in added lines of one new file, and Elder receives
that file in full. What is missing is the question "is this CHECK
sufficient?": what the assertion proves compared with what it claims, and
whether a passing counterexample exists. Queued as #1420.

### Reverse-direction completeness: reachable by a prompt rule

The forward check, the reverse check and the map are all added lines in
the same new file. The missing reasoning has two steps. A check asserts
A -> B; is B -> A also required? If so, is the B -> A set built from
live data or from an intermediary that can go stale? Queued as #1421.

Both slices change `code_review_prompt.py`, so each must also re-record
`elder_eval/baseline.json` for the new `prompt_sha`, and each adds a
negative fixture (the fixed attester as merged) that its rule must leave
clean.
