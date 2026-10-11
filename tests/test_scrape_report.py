"""A truncated scrape must say so.

The bug this file exists for: get_posts returned whatever it managed to collect
and said nothing about it. A profile with 60 posts yielded 4 (the GraphQL query
had started answering with the HTML app shell, so the DOM fallback took over
and stopped scrolling after two empty rounds), the CLI printed one line per
downloaded file, and the run ended looking exactly like a complete one.

Two halves are covered here:
  * download_media says whether it fetched bytes or merely recognised a file,
    so "descargados" in the summary cannot be a lie;
  * get_posts records why it stopped, so a partial walk is visible.
"""

import json

import httpx
from conftest import seed_template

import threadstractormf.rate_limit as rl
from threadstractormf.api import ThreadsAPI
from threadstractormf.archive import DownloadLedger, ledger_path_for
from threadstractormf.client import Threadscraper
from threadstractormf.downloader import DownloadStatus, download_media
from threadstractormf.models import Media

_IMAGE = {"candidates": [{"width": 1080, "url": "https://scontent.cdninstagram.com/v/a.jpg"}]}


def _page(codes, *, has_next=False, cursor=None, username="user"):
    edges = [
        {
            "node": {
                "thread_items": [
                    {
                        "post": {
                            "pk": str(1000 + i),
                            "code": code,
                            "user": {"username": username},
                            "caption": {"text": "t"},
                            "taken_at": 1700000000,
                            "image_versions2": _IMAGE,
                        }
                    }
                ]
            }
        }
        for i, code in enumerate(codes)
    ]
    media = {"edges": edges}
    media["page_info"] = {"has_next_page": has_next, "end_cursor": cursor}
    return {"data": {"mediaData": media}}


def _codes(n, prefix="P"):
    return [f"{prefix}{i}" for i in range(n)]


def _seed_template(api):
    """Give a mocked API a usable request template.

    Since 1.3 the doc_id is captured from the live web app rather than
    hardcoded, so a mocked transport has to supply one. Without this the walk
    would reach for Playwright, which is both slow and pointless against a
    MockTransport. Delegated to the shared helper in conftest so the shape
    (username key, relay variables) cannot drift from the rest of the suite.
    """
    return seed_template(api)


def _api(pages, *, user_id="999"):
    """A ThreadsAPI wired to a mocked GraphQL transport."""
    calls = {"n": 0}

    def handler(_request):
        i = calls["n"]
        calls["n"] += 1
        payload = pages[min(i, len(pages) - 1)]
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )

    api = ThreadsAPI({"csrftoken": "x"}, rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._resolve_user_id = lambda _u: user_id
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    _seed_template(api)
    # Stub the DOM fallback here rather than per test. These cases all end in
    # "GraphQL produced nothing", which is exactly when get_posts reaches for
    # the browser — unstubbed that launches Chromium and calls threads.com for
    # ~23s, to return posts the test never looks at. Tests that care about the
    # fallback still override this.
    api._fetch_posts_via_playwright = lambda _u, _l: []
    api._test_calls = calls  # type: ignore[attr-defined]
    return api


# --- the API answering with the web app instead of JSON ---------------------
#
# This is the real-world failure: POST /graphql/query returned 200 text/html,
# so resp.json() raised and the broad `except Exception` turned it into a
# silent slide into the DOM fallback.


def test_html_instead_of_json_is_reported_not_hidden():
    def handler(_request):
        return httpx.Response(
            200,
            text="<!DOCTYPE html><html lang='en'></html>",
            headers={"content-type": "text/html"},
        )

    api = ThreadsAPI({"csrftoken": "x"}, rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._resolve_user_id = lambda _u: "999"
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    _seed_template(api)
    api._fetch_posts_via_playwright = lambda _u, _l: []

    api.get_posts("user", limit=5)
    report = api.last_report
    assert report.used_fallback is True
    assert "JSON" in report.api_error, report.api_error
    # The cause must name the likely culprit, not just "it failed".
    assert "doc_id" in report.api_error


def test_the_warning_reaches_stderr_not_stdout(capsys):
    """stdout is the one-line-per-file contract; diagnostics must not go there."""

    def handler(_request):
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    api = ThreadsAPI({"csrftoken": "x"}, rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._resolve_user_id = lambda _u: "999"
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    _seed_template(api)
    api._fetch_posts_via_playwright = lambda _u, _l: []

    api.get_posts("user", limit=5)
    captured = capsys.readouterr()
    assert captured.out == "", "nothing may be written to stdout"
    assert "GraphQL" in captured.err


def test_a_working_query_reports_no_error():
    api = _api([_page(_codes(3))])
    posts = api.get_posts("user", limit=50)
    assert len(posts) == 3
    assert api.last_report.api_error == ""
    assert api.last_report.used_fallback is False


# --- why the walk stopped ---------------------------------------------------


def test_a_complete_walk_is_not_marked_truncated():
    api = _api([_page(_codes(3))])
    api.get_posts("user", limit=50)
    report = api.last_report
    assert report.truncated is False
    assert report.pages_fetched == 1
    assert report.posts == 3


def test_hitting_the_limit_says_so_instead_of_pretending_completion():
    api = _api([_page(_codes(10), has_next=True, cursor="c1")])
    api.get_posts("user", limit=4)
    report = api.last_report
    assert report.posts == 4
    assert "limit" in report.stop_reason


def test_a_repeated_cursor_is_recorded():
    pages = [
        _page(_codes(2), has_next=True, cursor="same"),
        _page(_codes(2, prefix="q"), has_next=True, cursor="same"),
    ]
    api = _api(pages)
    api.get_posts("user", limit=100)
    assert "cursor" in api.last_report.stop_reason


def test_missing_page_info_is_recorded():
    payload = {"data": {"mediaData": {"edges": []}}}  # no page_info at all
    api = _api([payload])
    api.get_posts("user", limit=100)
    assert api.last_report.stop_reason


def test_exhausting_the_page_budget_is_marked_truncated():
    """A server that always promises another page must still be bounded."""
    pages = [
        _page(_codes(2, prefix=f"p{i}_"), has_next=True, cursor=f"c{i}") for i in range(500)
    ]
    api = _api(pages)
    api.get_posts("user", limit=10**6)
    report = api.last_report
    assert report.truncated is True
    assert "budget" in report.stop_reason


# --- posts dropped on the floor --------------------------------------------


def test_excluded_reposts_are_counted_not_invisible():
    pages = [_page(_codes(4), has_next=False, username="somebody_else")]
    api = _api(pages)
    api.get_posts("user", limit=50)
    report = api.last_report
    assert report.reposts_skipped == 4, "silently dropping them was the bug"
    assert report.posts == 0


def test_unparseable_posts_are_counted():
    payload = {
        "data": {
            "mediaData": {
                "edges": [{"node": {}}, {"node": {"thread_items": []}}],
                "page_info": {"has_next_page": False},
            }
        }
    }
    api = _api([payload])
    api.get_posts("user", limit=50)
    assert api.last_report.unparsed == 2


def test_caption_only_posts_are_dropped_but_counted():
    """A post with no media is deliberately not returned — but say so.

    ``_parse_post_node`` returns None for a caption-only post on purpose (this
    is a media downloader). Before the report existed that post vanished with
    no trace, so a profile full of them looked like a scrape that worked.
    """
    no_media_node = {
        "node": {
            "thread_items": [
                {
                    "post": {
                        "pk": "7",
                        "code": "NOMEDIA",
                        "user": {"username": "user"},
                        "taken_at": 1700000000,
                    }
                }
            ]
        }
    }
    media = _page(["WITHMEDIA"])["data"]["mediaData"]
    media["edges"].append(no_media_node)
    api = _api([{"data": {"mediaData": media}}])

    posts = api.get_posts("user", limit=50)
    report = api.last_report
    assert [p.id for p in posts] == ["WITHMEDIA"], "text-only posts are not returned"
    assert report.unparsed == 1, "but the drop must be visible in the count"


# --- downloaded vs merely recognised ----------------------------------------


def _media(post_id="P1", ext="jpg"):
    return Media(
        id=post_id,
        post_id=post_id,
        index=1,
        type="image",
        url="https://scontent.cdninstagram.com/v/a.jpg",
        ext=ext,
    )


def _client():
    return httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"DATA"))
    )


def test_a_fresh_download_reports_downloaded(tmp_path):
    outcome = download_media(_media(), tmp_path, client=_client(), rate_limit=False)
    assert outcome.status is DownloadStatus.DOWNLOADED
    assert outcome.path.exists()


def test_a_file_the_ledger_knows_reports_skipped_not_downloaded(tmp_path):
    """The whole point: the summary must not count this as a download."""
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    first = download_media(
        _media(), tmp_path, client=_client(), rate_limit=False, ledger=ledger
    )
    assert first.status is DownloadStatus.DOWNLOADED

    second = download_media(
        _media(), tmp_path, client=_client(), rate_limit=False, ledger=ledger
    )
    assert second.status is DownloadStatus.SKIPPED
    assert second.path == first.path


def test_an_existing_file_is_adopted(tmp_path):
    outcome = download_media(_media(), tmp_path, client=_client(), rate_limit=False)
    again = download_media(_media(), tmp_path, client=_client(), rate_limit=False)
    assert again.status is DownloadStatus.ADOPTED
    assert again.path == outcome.path


def test_the_client_exposes_the_report(tmp_path):
    scraper = Threadscraper(cookies={"csrftoken": "x"}, rate_limit=False)
    try:
        assert scraper.last_report.posts == 0
    finally:
        scraper.close()


# --- the summary line -------------------------------------------------------


def test_summary_reports_truthful_counts(capsys):
    """`descargados` must mean bytes were fetched, not "we already had it"."""
    from threadstractormf.api import ScrapeReport
    from threadstractormf.cli import _report_scrape

    report = ScrapeReport(posts=4, media=29, truncated=True, stop_reason="test")
    report.downloaded = 12
    report.skipped = 17
    _report_scrape(report)
    err = capsys.readouterr().err
    assert "posts: 4" in err
    assert "downloaded: 12" in err
    assert "skipped: 17" in err
    assert "test" in err, "the stop reason must be shown"


def test_count_splits_downloaded_from_skipped():
    from pathlib import Path

    from threadstractormf.api import ScrapeReport
    from threadstractormf.cli import _count
    from threadstractormf.downloader import DownloadOutcome

    report = ScrapeReport()
    _count(DownloadOutcome(Path("x"), DownloadStatus.DOWNLOADED), report)
    _count(DownloadOutcome(Path("y"), DownloadStatus.SKIPPED), report)
    _count(DownloadOutcome(Path("z"), DownloadStatus.ADOPTED), report)
    assert report.downloaded == 1
    assert report.skipped == 2


def test_version_flag_exits_zero_and_prints_the_version():
    from typer.testing import CliRunner

    from threadstractormf import __version__
    from threadstractormf.cli import app

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_a_critical_graphql_error_is_not_read_as_an_empty_profile():
    """Threads rejects a stale doc_id with 200 JSON + errors, and no data.

    This is what actually happens in the wild: ``{"errors": [{"message":
    "execution error", "severity": "CRITICAL"}], "data": {}}``. It used to be
    indistinguishable from a profile with no posts, so the walk stopped in
    silence and the DOM fallback quietly returned a fraction of the timeline.
    """
    payload = {
        "errors": [{"message": "execution error", "severity": "CRITICAL"}],
        "data": {},
        "status": "fail",
    }
    api = _api([payload])
    api._fetch_posts_via_playwright = lambda _u, _l: []

    api.get_posts("user", limit=50)
    report = api.last_report
    assert report.api_error, "a rejected query must not look like an empty profile"
    assert "execution error" in report.api_error
    assert "rejected" in report.stop_reason


def test_a_genuinely_empty_payload_is_not_reported_as_an_error():
    """No errors and no mediaData is a legitimately empty profile."""
    api = _api([{"data": {"mediaData": None}}])
    api._fetch_posts_via_playwright = lambda _u, _l: []

    api.get_posts("user", limit=50)
    report = api.last_report
    assert report.api_error == ""
    assert report.stop_reason == "mediaData absent"


# --- the fallback's summary must not contradict itself ----------------------
#
# A browser scrape used to print two warnings that disagreed: "it reads the
# public profile in full" and, immediately after, "the walk stopped before
# exhausting the profile (the API rejected the query)". Both were true of the
# two different stages, and neither of the stage that actually produced the
# posts.


def _summary(report, capsys):
    from threadstractormf.cli import _report_scrape

    _report_scrape(report)
    return capsys.readouterr().err


def test_the_fallback_does_not_also_claim_the_walk_was_cut_short(capsys):
    from threadstractormf.api import ScrapeReport

    report = ScrapeReport(posts=9, media=54, truncated=True, used_fallback=True)
    report.fallback_reason = "the profile stopped rendering new posts"
    err = _summary(report, capsys)

    assert "the walk stopped before exhausting" not in err, err
    # But truncation is not hidden: it is folded into the fallback message.
    assert "stopped because the profile stopped rendering" in err


def test_a_truncated_api_walk_still_warns_on_its_own(capsys):
    """The guard must not swallow the warning when no fallback was involved."""
    from threadstractormf.api import ScrapeReport

    report = ScrapeReport(posts=4, media=29, truncated=True, stop_reason="page budget")
    err = _summary(report, capsys)

    assert "the walk stopped before exhausting" in err
    assert "page budget" in err


def test_the_fallback_message_names_its_own_reason_not_the_apis(capsys):
    from threadstractormf.api import ScrapeReport

    report = ScrapeReport(
        posts=9,
        media=54,
        truncated=True,
        used_fallback=True,
        api_error="GraphQL returned errors: execution error",
        stop_reason="the API rejected the query",
    )
    report.fallback_reason = "the scroll ceiling was reached"
    err = _summary(report, capsys)

    # The API error survives: it is still why we left the fast path.
    assert "the API did not respond" in err
    assert "execution error" in err
    # But it is not quoted as the reason the walk stopped.
    assert "the walk stopped before exhausting" not in err
    assert "It stopped because the scroll ceiling was reached" in err
    assert "(the API rejected the query)" not in err


def test_coerce_dom_accepts_the_plain_lists_tests_stub_with():
    """Thirty-odd tests stub the fallback with a bare list; they must keep working."""
    from threadstractormf.api import DomScrape, coerce_dom

    assert coerce_dom([]).posts == []
    assert coerce_dom(["DOM"]).posts == ["DOM"]
    real = DomScrape(posts=[1], exhausted=True, reason="r")
    assert coerce_dom(real) is real
