"""Permission failures on Elder's explicit rerun PR read."""

from unittest.mock import MagicMock

import github_app_auth
import httpx
import interactive
import rerun


def test_improve_pr_read_403_reports_permission_failure(monkeypatch, caplog):
    url = "https://api.github.com/repos/myorg/myrepo/pulls/7"
    denied = httpx.Response(
        403, headers={"X-GitHub-Request-Id": "TEST:403"},
        request=httpx.Request("GET", url),
    )
    read_pr = MagicMock(return_value=denied)
    post_check = MagicMock(return_value={"id": 1})
    dispatch = MagicMock()
    settle_reaction = MagicMock()
    sqs = MagicMock()
    monkeypatch.setattr(rerun, "_sqs", sqs)
    monkeypatch.setattr(rerun, "_RERUN_QUEUE_URL", "test-queue")
    monkeypatch.setattr(interactive, "_elder_review_state", lambda **kwargs: "idle")
    monkeypatch.setattr(interactive, "_mark_reviewing", MagicMock())
    monkeypatch.setattr(interactive, "_react", MagicMock())
    monkeypatch.setattr(github_app_auth, "get_install_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(rerun.httpx, "get", read_pr)
    monkeypatch.setattr(rerun, "post_check_run", post_check)
    monkeypatch.setattr(rerun, "dispatch_code_review", dispatch)
    monkeypatch.setattr(rerun, "_settle_command_reaction", settle_reaction)
    head_sha = "a" * 40
    interactive.run_command(
        "improve", "", install_id=11, owner="myorg", repo="myrepo", pr_number=7,
        comment_id=99, token_fn=lambda fn: fn("token"), claim=lambda _: True,
        head_sha=head_sha,
    )
    body = sqs.send_message.call_args.kwargs["MessageBody"]

    result = rerun.handle_rerun_jobs({"Records": [{"body": body}]})

    assert result == {"records": 1, "dispatched": 0, "skipped": 1}
    assert [call.args[0] for call in read_pr.call_args_list] == [url, url]
    post_check.assert_called_once()
    check = post_check.call_args.args[3]
    assert check.title == "Grug cannot read this pull request: 403 from GitHub"
    assert check.head_sha == head_sha
    assert check.conclusion == "failure"
    dispatch.assert_not_called()
    settle_reaction.assert_called_once()
    assert settle_reaction.call_args.kwargs["done"] is False
    sqs.send_message.assert_called_once()
    [record] = [r for r in caplog.records if r.message == "elder_pr_read_forbidden"]
    assert (record.install_id, record.repo, record.pr, record.status) == (
        11, "myorg/myrepo", 7, 403,
    )
    assert record.github_request_id == "TEST:403"
