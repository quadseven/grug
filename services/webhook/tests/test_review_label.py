"""grug#1270: the `grug:reviewing` label - the visible sign that Elder is
re-reviewing a PR - and the sweep that clears the labels that outlived
their review.

The Elder CHECK RUN is the source of truth for "a review is running"; the
label is only the sign a person (or an agent reading the PR page) can see.
"""

from __future__ import annotations

import httpx
import pytest

import review_label


class _Resp:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _FakeHttp:
    """Records every call; models a repo with or without the label."""

    def __init__(self, *, label_exists: bool = True, delete_status: int = 204) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.label_exists = label_exists
        self.delete_status = delete_status

    def get(self, url: str, **kw: object) -> _Resp:
        self.calls.append(("get", url, None))
        if "/labels/" in url:
            return _Resp(200 if self.label_exists else 404)
        return _Resp(200, {"items": []})

    def post(self, url: str, **kw: object) -> _Resp:
        self.calls.append(("post", url, kw.get("json")))
        if "/issues/" not in url and url.endswith("/labels"):
            # Creating the label in the repo.
            self.label_exists = True
            return _Resp(201)
        # GitHub answers 422 for a label name the repo does not have.
        return _Resp(200 if self.label_exists else 422)

    def delete(self, url: str, **kw: object) -> _Resp:
        self.calls.append(("delete", url, None))
        return _Resp(self.delete_status)


def _runs(status: str, conclusion: str = "") -> list[dict]:
    run: dict = {"name": "Grug - Elder", "status": status}
    if conclusion:
        run["conclusion"] = conclusion
    return [run]


# --- the label writes ------------------------------------------------------


def test_add_reviewing_label_attaches_the_label(monkeypatch):
    fake = _FakeHttp()
    monkeypatch.setattr(review_label, "httpx", fake)
    assert review_label.add_reviewing_label(
        token="tok", owner="o", repo="r", pr_number=7,
    ) is True
    attach = [c for c in fake.calls if c[0] == "post"]
    assert len(attach) == 1
    assert attach[0][1].endswith("/repos/o/r/issues/7/labels")
    assert attach[0][2] == {"labels": ["grug:reviewing"]}


def test_add_reviewing_label_creates_the_label_when_the_repo_has_none(monkeypatch):
    """A repo that has never been reviewed by grug has no such label, and
    GitHub rejects an unknown label name - so the label is created on
    demand."""
    fake = _FakeHttp(label_exists=False)
    monkeypatch.setattr(review_label, "httpx", fake)
    assert review_label.add_reviewing_label(
        token="tok", owner="o", repo="r", pr_number=7,
    ) is True
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["post", "get", "post", "post"]
    created = fake.calls[2]
    assert created[1].endswith("/repos/o/r/labels")
    assert created[2]["name"] == "grug:reviewing"


def test_remove_reviewing_label_tolerates_a_label_that_is_not_there(monkeypatch):
    """The goal is "the PR does not carry the label", not "the DELETE call
    succeeded"."""
    fake = _FakeHttp(delete_status=404)
    monkeypatch.setattr(review_label, "httpx", fake)
    assert review_label.remove_reviewing_label(
        token="tok", owner="o", repo="r", pr_number=7,
    ) is True
    assert fake.calls[0][0] == "delete"
    assert "grug%3Areviewing" in fake.calls[0][1]


# --- the Elder check on a head: the source of truth ------------------------


def test_elder_check_state_is_running_while_the_review_works(monkeypatch):
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref", lambda *a, **k: _runs("in_progress"),
    )
    assert review_label.elder_check_state(
        token="tok", owner="o", repo="r", head_sha="a" * 40,
    ) == "running"


def test_elder_check_state_is_concluded_once_the_check_finished(monkeypatch):
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref",
        lambda *a, **k: _runs("completed", "success"),
    )
    assert review_label.elder_check_state(
        token="tok", owner="o", repo="r", head_sha="a" * 40,
    ) == "concluded"


def test_elder_check_state_is_absent_without_any_elder_run(monkeypatch):
    monkeypatch.setattr(review_label, "list_check_runs_for_ref", lambda *a, **k: [])
    assert review_label.elder_check_state(
        token="tok", owner="o", repo="r", head_sha="a" * 40,
    ) == "absent"


def test_elder_check_state_is_unknown_when_github_cannot_be_read(monkeypatch):
    """An unreadable state must never be read as "no review running" - that
    would strip the label off a PR Elder is working on."""
    def _boom(*a: object, **k: object) -> list[dict]:
        raise httpx.HTTPError("github down")

    monkeypatch.setattr(review_label, "list_check_runs_for_ref", _boom)
    assert review_label.elder_check_state(
        token="tok", owner="o", repo="r", head_sha="a" * 40,
    ) == "unknown"


def test_a_running_elder_run_wins_over_a_completed_one(monkeypatch):
    """Both names (the current title and a legacy alias) can carry a run at
    once; "something is still running" is the answer that keeps the sign."""
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref",
        lambda *a, **k: [
            {"name": "Grug - Code Review", "status": "completed", "conclusion": "success"},
            {"name": "Grug - Elder", "status": "in_progress"},
        ],
    )
    assert review_label.elder_check_state(
        token="tok", owner="o", repo="r", head_sha="a" * 40,
    ) == "running"


@pytest.mark.parametrize("conclusion", ["success", "failure", "neutral"])
def test_elder_check_conclusion_reports_the_terminal_conclusion(monkeypatch, conclusion):
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref",
        lambda *a, **k: _runs("completed", conclusion),
    )
    assert review_label.elder_check_conclusion(
        token="tok", owner="o", repo="r", head_sha="a" * 40,
    ) == conclusion


# --- the sweep: no label may outlive its review ----------------------------


def _search_response(items: list[dict]) -> _Resp:
    return _Resp(200, {"items": items})


def test_sweep_removes_a_label_whose_head_has_no_in_progress_check(monkeypatch):
    """grug#1270 AC3: a stale label with no in-progress Elder check is
    removed by the sweep - one missed removal must never block a future
    run."""
    fake = _FakeHttp()
    monkeypatch.setattr(review_label, "httpx", fake)
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref",
        lambda *a, **k: _runs("completed", "success"),
    )
    search_items = [
        {
            "repository_url": "https://api.github.com/repos/myorg/myrepo",
            "number": 7,
        }
    ]
    monkeypatch.setattr(
        review_label.httpx, "get",
        lambda url, **kw: _search_response(search_items)
        if "/search/issues" in url
        else _Resp(200, {"head": {"sha": "b" * 40}}),
    )
    removed = review_label.sweep_repo_labels(
        token="tok", owner="myorg", repo="myrepo", pr_numbers=[7],
    )
    assert removed == 1
    assert [c[0] for c in fake.calls] == ["delete"]
    assert "/issues/7/labels/" in fake.calls[0][1]


def test_sweep_keeps_the_label_while_a_review_is_in_flight(monkeypatch):
    fake = _FakeHttp()
    monkeypatch.setattr(review_label, "httpx", fake)
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref", lambda *a, **k: _runs("in_progress"),
    )
    monkeypatch.setattr(
        review_label.httpx, "get",
        lambda url, **kw: _Resp(200, {"head": {"sha": "b" * 40}}),
    )
    removed = review_label.sweep_repo_labels(
        token="tok", owner="myorg", repo="myrepo", pr_numbers=[7],
    )
    assert removed == 0
    assert fake.calls == []


def test_sweep_keeps_the_label_when_github_cannot_be_read(monkeypatch):
    """GitHub said nothing, so the label stays: guessing "no review" would
    strip the sign off a PR Elder is working on."""
    fake = _FakeHttp()
    monkeypatch.setattr(review_label, "httpx", fake)

    def _boom(*a: object, **k: object) -> list[dict]:
        raise httpx.HTTPError("github down")

    monkeypatch.setattr(review_label, "list_check_runs_for_ref", _boom)
    monkeypatch.setattr(
        review_label.httpx, "get",
        lambda url, **kw: _Resp(200, {"head": {"sha": "b" * 40}}),
    )
    removed = review_label.sweep_repo_labels(
        token="tok", owner="myorg", repo="myrepo", pr_numbers=[7],
    )
    assert removed == 0
    assert fake.calls == []


def test_sweep_finds_the_labeled_prs_of_every_repo(monkeypatch):
    """One call per repo of the installation - the same per-repo shape the
    enforcement pass already uses."""
    seen: list[dict] = []

    def _get(url: str, **kw: object) -> _Resp:
        seen.append(dict(kw.get("params") or {}))  # type: ignore[call-overload]
        if url.endswith("/issues"):
            return _Resp(200, [{"number": 7}])
        return _Resp(200, {"head": {"sha": "b" * 40}})

    monkeypatch.setattr(review_label, "httpx", _FakeHttp())
    monkeypatch.setattr(review_label.httpx, "get", _get)
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref",
        lambda *a, **k: _runs("completed", "neutral"),
    )
    monkeypatch.setattr(
        review_label, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    monkeypatch.setattr(
        "github_rulesets_client.list_installation_repos",
        lambda token: [{"full_name": "myorg/myrepo"}],
    )
    removed = review_label.sweep_stale_labels(install_id=11)
    assert removed == 1
    assert seen and seen[0].get("labels") == "grug:reviewing"
    assert seen[0].get("state") == "all"


def test_the_sweep_stops_at_its_cap(monkeypatch):
    """One pass is bounded: a repo with hundreds of stale labels must not
    turn one sweep into an unbounded crawl. What the cap leaves behind is
    swept by the next pass."""
    fake = _FakeHttp()
    monkeypatch.setattr(review_label, "httpx", fake)
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref",
        lambda *a, **k: _runs("completed", "success"),
    )
    monkeypatch.setattr(
        review_label.httpx, "get",
        lambda url, **kw: _Resp(200, {"head": {"sha": "b" * 40}}),
    )
    removed = review_label.sweep_repo_labels(
        token="tok", owner="myorg", repo="myrepo", pr_numbers=[7, 8, 9],
        max_removals=1,
    )
    assert removed == 1
    assert len([c for c in fake.calls if c[0] == "delete"]) == 1


def _two_repo_sweep(monkeypatch, *, broken_repo: str = "") -> _FakeHttp:
    """Two repos of one installation, each with three labeled PRs whose
    Elder check concluded. `broken_repo` answers its PR listing with an
    error, the way a repo the App lost access to would."""
    fake = _FakeHttp()

    def _get(url: str, **kw: object) -> _Resp:
        if url.endswith("/issues"):
            if broken_repo and f"/{broken_repo}/" in url:
                raise httpx.ConnectError("listing unreadable")
            return _Resp(200, [{"number": n} for n in (7, 8, 9)])
        return _Resp(200, {"head": {"sha": "b" * 40}})

    monkeypatch.setattr(review_label, "httpx", fake)
    monkeypatch.setattr(review_label.httpx, "get", _get)
    monkeypatch.setattr(
        review_label, "list_check_runs_for_ref",
        lambda *a, **k: _runs("completed", "success"),
    )
    monkeypatch.setattr(
        review_label, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    monkeypatch.setattr(
        "github_rulesets_client.list_installation_repos",
        lambda token: [{"full_name": "myorg/first"}, {"full_name": "myorg/second"}],
    )
    return fake


def test_the_cap_holds_across_the_repos_of_one_pass(monkeypatch):
    """The cap is per pass, not per repo: the second repo gets only what the
    first left of it, and the pass never removes more than the cap."""
    fake = _two_repo_sweep(monkeypatch)
    removed = review_label.sweep_stale_labels(install_id=11, max_removals=4)
    deletes = [c[1] for c in fake.calls if c[0] == "delete"]
    assert removed == 4
    assert len(deletes) == 4
    assert len([u for u in deletes if "/myorg/first/" in u]) == 3
    assert len([u for u in deletes if "/myorg/second/" in u]) == 1


def test_one_unreadable_repo_does_not_stop_the_sweep(monkeypatch):
    """Best-effort per repo: a repo whose labeled-PR listing fails is
    skipped, and the installation's other repos are still swept."""
    fake = _two_repo_sweep(monkeypatch, broken_repo="first")
    removed = review_label.sweep_stale_labels(install_id=11)
    deletes = [c[1] for c in fake.calls if c[0] == "delete"]
    assert removed == 3
    assert all("/myorg/second/" in u for u in deletes)
