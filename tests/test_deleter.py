"""Offline tests for the core search/delete loop (no network).

Run with either:
    python -m pytest
    python tests/test_deleter.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undiscord.deleter import MessageDeleter, DeleteOptions, to_snowflake


def opts(**kw):
    base = dict(auth_token="t", guild_id="@me", channel_id="c",
                delay_min_ms=0, delay_max_ms=0, network_retries=1)
    base.update(kw)
    return DeleteOptions(**base)


def msg(i, type=0, pinned=False, channel="c", attachments=None):
    return {"id": str(i), "type": type, "pinned": pinned, "channel_id": channel,
            "content": f"m{i}", "timestamp": "2023-01-01T00:00:00.000000+00:00",
            "author": {"username": "me", "discriminator": "0"},
            "attachments": attachments or [], "hit": True}


def search(total, msgs):
    return 200, {"total_results": total, "messages": [[m] for m in msgs]}


def test_happy():
    d = MessageDeleter(opts())
    responses = iter([search(2, [msg(1), msg(2)]), (204, None), (204, None)])
    d._request = lambda method, url: next(responses)
    stats = d.run()
    assert stats.deleted == 2
    assert stats.failed == 0


def test_rate_limit():
    d = MessageDeleter(opts())
    responses = iter([search(1, [msg(1)]), (429, {"retry_after": 1}), (204, None)])
    d._request = lambda method, url: next(responses)
    stats = d.run()
    assert stats.deleted == 1
    assert stats.throttled_count == 1


def test_archived():
    d = MessageDeleter(opts())
    responses = iter([
        search(2, [msg(1, channel="thread"), msg(2, channel="thread")]),
        (400, {"code": 50083, "message": "archived"}),
        search(2, [msg(1, channel="thread"), msg(2, channel="thread")]),
    ])
    d._request = lambda method, url: next(responses)
    stats = d.run()
    assert "thread" in d.archived_threads
    assert stats.deleted == 0
    assert stats.failed == 2


def test_dry_run():
    d = MessageDeleter(opts(dry_run=True))
    calls = {"delete": 0}
    page = iter([search(2, [msg(1), msg(2)]), search(2, [])])

    def req(method, url):
        if method == "DELETE":
            calls["delete"] += 1
        return next(page)

    d._request = req
    stats = d.run()
    assert calls["delete"] == 0
    assert stats.deleted == 2


def test_snowflake():
    assert to_snowflake("123456789") == "123456789"
    sn = to_snowflake("2023-01-01T00:00")
    assert sn.isdigit() and int(sn) > 0


def test_pinned_filter():
    # A pinned, non-default-type message is only deletable with include_pinned.
    d = MessageDeleter(opts())
    responses = iter([search(1, [msg(1, type=21, pinned=True)]), search(1, [])])
    d._request = lambda method, url: next(responses)
    stats = d.run()
    assert stats.deleted == 0  # not deleted: not type 0/6 and include_pinned=False

    d2 = MessageDeleter(opts(include_pinned=True))
    responses2 = iter([search(1, [msg(1, type=21, pinned=True)]), (204, None)])
    d2._request = lambda method, url: next(responses2)
    stats2 = d2.run()
    assert stats2.deleted == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("\nALL TESTS PASSED")
