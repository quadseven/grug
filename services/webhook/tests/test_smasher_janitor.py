"""Trial orphan-janitor tests (#469, codex peer-review PR #494).

`reap_orphans` deletes app=grug-trial PVC/Secret/Job objects older than the
cutoff so a crashed launcher can't leave private-checkout PVCs or token Secrets
behind. Injectable clock + cluster - no live API server."""

from __future__ import annotations

from personas.smasher.trial_janitor import reap_orphans

_NOW = 1_800_000_000.0  # fixed "now" (epoch seconds)


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeCluster:
    def __init__(self, orphans):
        self._orphans = orphans
        self.deleted: list = []

    def list_orphans(self):
        return self._orphans

    def delete(self, kind, name):
        self.deleted.append((kind, name))


def test_reaps_old_resources_keeps_fresh():
    cluster = _FakeCluster([
        ("pvc", "old-ws", _iso(_NOW - 3600)),       # 1h old -> reap
        ("secret", "old-token", _iso(_NOW - 3600)),  # 1h old -> reap
        ("pvc", "fresh-ws", _iso(_NOW - 60)),        # 1min old -> keep (live Trial)
        ("job", "old-prep", _iso(_NOW - 7200)),      # 2h old -> reap
    ])
    result = reap_orphans(cluster=cluster, now_epoch=_NOW, max_age_seconds=1800)
    assert set(cluster.deleted) == {("pvc", "old-ws"), ("secret", "old-token"), ("job", "old-prep")}
    assert ("pvc", "fresh-ws") not in cluster.deleted
    assert result == {"pvc": 1, "secret": 1, "job": 1}


def test_unparseable_timestamp_is_kept_never_deleted_blind():
    cluster = _FakeCluster([("pvc", "weird", "not-a-timestamp")])
    reap_orphans(cluster=cluster, now_epoch=_NOW, max_age_seconds=1800)
    assert cluster.deleted == []


def test_list_failure_is_a_noop_not_a_crash():
    class _Boom:
        def list_orphans(self):
            raise RuntimeError("api down")

        def delete(self, kind, name):
            pass

    result = reap_orphans(cluster=_Boom(), now_epoch=_NOW, max_age_seconds=1800)
    assert result == {"pvc": 0, "secret": 0, "job": 0}


def test_one_bad_delete_does_not_stop_the_sweep():
    class _PartialBoom(_FakeCluster):
        def delete(self, kind, name):
            if name == "boom":
                raise RuntimeError("delete failed")
            super().delete(kind, name)

    cluster = _PartialBoom([
        ("pvc", "boom", _iso(_NOW - 3600)),
        ("pvc", "ok", _iso(_NOW - 3600)),
    ])
    reap_orphans(cluster=cluster, now_epoch=_NOW, max_age_seconds=1800)
    assert ("pvc", "ok") in cluster.deleted  # the good one still got reaped
