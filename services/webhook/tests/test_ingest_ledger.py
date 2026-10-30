"""#361 slice 1: ledger ingest - seq disambiguation, skip counting."""

from __future__ import annotations

import json

from ingest_ledger import ingest_text


def _line(**kw):
    base = dict(repo="quadseven/grug", pr=1, reviewer="codex",
                severity="HIGH", finding="f", verdict="fixed")
    base.update(kw)
    return json.dumps({**base, "class": kw.get("finding_class", "silent-failure")})


def test_ingest_persists_valid_and_counts_skips():
    puts = []
    text = "\n".join([
        _line(pr=1),
        "",                 # blank -> skip (not counted)
        "{bad json",        # malformed -> skipped
        json.dumps({"repo": "r"}),  # missing fields -> skipped
        _line(pr=2),
    ])
    res = ingest_text(text, put=lambda row: puts.append(row["pr"]), put_practices=lambda repo, ps: None,
                      put_exemplars=lambda repo, ex: None)
    assert res["ingested"] == 2 and res["skipped"] == 2
    assert puts == [1, 2]


def test_all_valid_rows_are_put():
    puts = []
    # two findings sharing (repo, class, pr, reviewer) but different content
    # -> both persisted (content-derived key disambiguates, no seq needed)
    text = "\n".join([_line(pr=5, finding="a"), _line(pr=5, finding="b")])
    ingest_text(text, put=lambda row: puts.append(row["finding"]), put_practices=lambda repo, ps: None,
                      put_exemplars=lambda repo, ex: None)
    assert puts == ["a", "b"]


def test_ingest_is_reusable_across_classes():
    puts = []
    text = "\n".join([
        _line(pr=5, finding_class="silent-failure"),
        _line(pr=5, finding_class="correctness"),
    ])
    ingest_text(text, put=lambda row: puts.append(row["class"]), put_practices=lambda repo, ps: None,
                      put_exemplars=lambda repo, ex: None)
    assert puts == ["silent-failure", "correctness"]


def test_ingest_comment_table_persists_rows_and_counts_skips():
    from ingest_ledger import ingest_comment_table

    body = (
        "| reviewer | severity | class | finding | verdict | evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| codex | high | silent-failure | f | fixed | e |\n"
        "| codex | high | silent-failure | g |\n"
    )
    puts = []
    out = ingest_comment_table(
        body, "o/r", 4, put=puts.append,
        put_practices=lambda *a: None, put_exemplars=lambda *a: None,
    )
    assert out["ingested"] == 1
    assert [p["pr"] for p in puts] == [4]
    assert puts[0]["repo"] == "o/r"


def test_main_comment_flag_ingests_a_comment_body_file(tmp_path, monkeypatch, capsys):
    import ingest_ledger

    body = tmp_path / "comment.md"
    body.write_text("| reviewer | severity | class | finding | verdict |\n", encoding="utf-8")
    seen = {}

    def fake(text, repo, pr):
        seen.update(text=text, repo=repo, pr=pr)
        return {"ingested": 2, "skipped": 0, "repos_refreshed": 1, "exemplars_refreshed": 1}

    monkeypatch.setattr(ingest_ledger, "ingest_comment_table", fake)
    rc = ingest_ledger.main(["x", "--comment", str(body), "--repo", "o/r", "--pr", "4"])
    assert rc == 0
    assert (seen["repo"], seen["pr"]) == ("o/r", 4)
    assert "2 ingested" in capsys.readouterr().out


def test_main_comment_flag_requires_repo(tmp_path):
    import ingest_ledger

    body = tmp_path / "comment.md"
    body.write_text("x", encoding="utf-8")
    assert ingest_ledger.main(["x", "--comment", str(body)]) == 2
