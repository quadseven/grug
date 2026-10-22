Not valid against this code: the two readers share no mutable state, so there
is no race to synchronize.

`_elder_check_already_terminal_or_pending` (services/webhook/rerun.py:263)
and the sweep (consumer.py -> review_label.sweep_stale_labels) both READ
GitHub's check-run listing for a head. The Elder CHECK RUN is the single
source of truth for "a review is running", and the `grug:reviewing` label is
explicitly not a lock: the command path's guard
(`interactive._elder_review_state`) asks GitHub for the check runs and never
looks at the label. The worst interleaving between the two readers is a
MISSING sign - the sweep removes a label the dispatch had just added - never
an overlapping review, and the sweep's next pass puts the sign back.

A shared snapshot is not something both can hold: the command path's guard
runs in the webhook service and the sweep runs in the consumer service (two
processes, no shared memory), and the alternative the finding suggests -
serializing the decision under a lock - would mean holding that lock across
GitHub API calls, which would stall Elder reviews for as long as the sweep
takes.

What the shared helper does buy is agreement: both sides now read the SAME
listing through `list_check_runs_for_ref` (same `filter=latest`, same
pagination), instead of the guard using its own hand-rolled page walk. That
is why the label and the guard can no longer disagree about what is running
on a head.
