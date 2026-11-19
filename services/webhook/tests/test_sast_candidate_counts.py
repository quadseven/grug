from __future__ import annotations

from pathlib import Path

from personas.code_reviewer.sast import Candidate
from sast_candidate_counts import candidate_counts, load_planted


def test_candidate_counts_run_planted_fixtures_through_opengrep_scanner():
    fixtures = load_planted()
    language_for_suffix = {
        ".py": "python",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".swift": "swift",
    }
    totals = {"python": 0, "yaml": 0, "swift": 0}
    first_fixture_by_language = {}
    for path, _ in fixtures.values():
        language = language_for_suffix[Path(path).suffix]
        totals[language] += 1
        first_fixture_by_language.setdefault(language, path)

    calls = []

    def scan(hunks, *, file_contents, engine):
        calls.append((hunks, file_contents, engine))
        return tuple(
            Candidate("sample", path, 1, "sample")
            for path in first_fixture_by_language.values()
        )

    counts = candidate_counts(fixtures, scanner=scan)

    assert counts == {
        language: (int(total > 0), total) for language, total in totals.items()
    }
    assert len(calls) == 1
    hunks, file_contents, engine = calls[0]
    assert engine == "opengrep"
    assert set(file_contents) == {path for path, _ in fixtures.values()}
    assert len(hunks) == len(fixtures)
