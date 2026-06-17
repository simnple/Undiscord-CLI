"""Core Discord message search + delete logic.

This is a faithful port of the ``deleteMessages`` routine from gen3vra's
``deletediscordmessages`` userscript (https://github.com/gen3vra/deletediscordmessages),
adapted for a synchronous command-line environment.

It reproduces the original search/delete loop, the randomized delays, the
rate-limit backoff (HTTP 202 "not indexed" and 429 "too fast"), and the
archived-thread / system-message handling. The browser-only bits (UI, token
scraping via webpack/localStorage) are intentionally dropped — the CLI takes
those as configuration instead.

The recursive ``recurse()`` of the original is rewritten as an explicit loop so
that deleting tens of thousands of messages cannot blow the Python stack.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

# 2015-01-01T00:00:00Z, the epoch Discord snowflakes count from.
DISCORD_EPOCH_MS = 1420070400000

# Message types we treat as deletable user content:
#   0  = DEFAULT
#   6  = CHANNEL_PINNED_MESSAGE (the "pinned a message" notice)
#   19 = REPLY  -- a normal message that replies to another. The original
#        userscript only allowed {0, 6}, so REPLY messages were wrongly skipped
#        (counted as "system") and left behind. They are ordinary, deletable.
DELETABLE_TYPES = {0, 6, 19}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def ms_to_hms(ms: float) -> str:
    """Format a duration in milliseconds as ``"1h 2m 3s"`` (matches msToHMS)."""
    ms = int(ms)
    h = ms // 3_600_000
    m = (ms % 3_600_000) // 60_000
    s = (ms % 60_000) // 1000
    return f"{h}h {m}m {s}s"


def _parse_datetime(value: str) -> datetime:
    """Parse a ``datetime-local`` / ISO string the way the browser ``new Date()`` does.

    A naive datetime (no timezone) is interpreted as *local* time, matching how
    the browser treats the userscript's ``datetime-local`` inputs.
    """
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach the local timezone
    return dt


def to_snowflake(value: Optional[str]) -> Optional[str]:
    """Port of the userscript's ``toSnowflake``.

    If ``value`` looks like a date (contains ``:``) convert it to a snowflake id,
    otherwise assume it already is a snowflake and return it unchanged.
    """
    if value is None or value == "":
        return None
    value = str(value)
    if ":" in value:
        ms = int(_parse_datetime(value).timestamp() * 1000)
        return str((ms - DISCORD_EPOCH_MS) << 22)
    return value


class DiscordAPIError(Exception):
    """Raised when an enumeration call returns a non-200 / unexpected response."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        msg = body.get("message") if isinstance(body, dict) else None
        super().__init__(f"Discord API error {status}: {msg or body}")


def _api_base_url(api_version: int) -> str:
    return f"https://discord.com/api/v{api_version}"


def _http_request(method, url, token, user_agent, timeout=30.0):
    """Bare HTTP request returning ``(status_code, parsed_json_or_None)``.

    Raises ``urllib.error.URLError`` only on transport failure; HTTP error
    statuses are returned with their parsed body.
    """
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": token, "User-Agent": user_agent, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    body = None
    if raw:
        try:
            body = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            body = None
    return status, body


def fetch_guilds(token, *, api_version=9, user_agent=DEFAULT_USER_AGENT, timeout=30.0) -> list:
    """List the guilds (servers) the account is a member of via /users/@me/guilds."""
    url = f"{_api_base_url(api_version)}/users/@me/guilds"
    status, body = _http_request("GET", url, token, user_agent, timeout)
    if status != 200 or not isinstance(body, list):
        raise DiscordAPIError(status, body)
    return body


def fetch_dm_channels(token, *, api_version=9, user_agent=DEFAULT_USER_AGENT, timeout=30.0) -> list:
    """List open DM / group-DM channels via /users/@me/channels.

    Note: only *open* DMs are returned; closed conversations won't appear. Use a
    data-export ``messages/index.json`` (import-index) to reach those.
    """
    url = f"{_api_base_url(api_version)}/users/@me/channels"
    status, body = _http_request("GET", url, token, user_agent, timeout)
    if status != 200 or not isinstance(body, list):
        raise DiscordAPIError(status, body)
    return body


@dataclass
class DeleteOptions:
    """Everything needed to run one channel's delete pass."""

    auth_token: str
    author_id: Optional[str] = None
    guild_id: Optional[str] = None          # use "@me" for DMs / group DMs
    channel_id: Optional[str] = None
    min_id: Optional[str] = None            # snowflake or ISO date (After)
    max_id: Optional[str] = None            # snowflake or ISO date (Before)
    content: Optional[str] = None
    has_link: bool = False
    has_file: bool = False
    include_nsfw: bool = False
    include_pinned: bool = False

    dry_run: bool = False
    api_version: int = 9
    user_agent: str = DEFAULT_USER_AGENT
    delay_min_ms: int = 1000
    delay_max_ms: int = 2000
    request_timeout: float = 30.0
    network_retries: int = 3


@dataclass
class DeleteStats:
    """Outcome of a run, returned to the caller."""

    deleted: int = 0
    failed: int = 0
    archived_skipped: int = 0
    throttled_count: int = 0
    throttled_total_ms: float = 0
    grand_total: int = 0
    elapsed_ms: float = 0
    stopped: bool = False
    aborted: bool = False


# Logger receives (level, message, extra) where level is one of
# debug/info/verb/warn/error/success. extra is an optional second string.
Logger = Callable[..., None]
# StopCheck returns True when the run should stop.
StopCheck = Callable[[], bool]
# ConfirmFn receives (total, etr_str, preview_messages) -> bool
ConfirmFn = Callable[[int, str, list], bool]
# ProgressFn receives (value, maximum, has_undeletable)
ProgressFn = Callable[..., None]


class MessageDeleter:
    """Search for and delete messages in a single channel."""

    def __init__(
        self,
        opts: DeleteOptions,
        logger: Optional[Logger] = None,
        stop_check: Optional[StopCheck] = None,
        confirm: Optional[ConfirmFn] = None,
        on_progress: Optional[ProgressFn] = None,
    ):
        self.opts = opts
        self._log_fn = logger
        self._stop_check = stop_check or (lambda: False)
        self._confirm = confirm
        self._on_progress = on_progress

    # ---- small helpers --------------------------------------------------- #

    def _log(self, level: str, *args: object) -> None:
        if self._log_fn:
            self._log_fn(level, *args)

    def _should_stop(self) -> bool:
        try:
            return bool(self._stop_check())
        except Exception:
            return False

    def _progress(self, value: int, maximum: int, has_undeletable: bool = False) -> None:
        if self._on_progress:
            try:
                self._on_progress(value, maximum, has_undeletable)
            except Exception:
                pass

    def _rand_delay(self) -> int:
        lo, hi = self.opts.delay_min_ms, self.opts.delay_max_ms
        if hi < lo:
            lo, hi = hi, lo
        return random.randint(lo, hi)

    def _wait(self, ms: float) -> None:
        """Sleep for ``ms`` milliseconds, but wake early (in <=250ms steps) if asked to stop."""
        deadline = time.monotonic() + (ms / 1000.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._should_stop():
                return
            time.sleep(min(remaining, 0.25))

    def _print_delay_stats(self) -> None:
        self._log(
            "debug",
            f"Delete delay: {self.delete_delay:.0f}ms, Search delay: {self.search_delay:.0f}ms",
            f"Last ping: {self.last_ping}ms, Avg ping: {self.avg_ping:.0f}ms",
        )

    # ---- HTTP ------------------------------------------------------------ #

    @property
    def _api_base(self) -> str:
        return f"https://discord.com/api/v{self.opts.api_version}"

    def _request(self, method: str, url: str) -> tuple[int, Optional[dict]]:
        """Perform an HTTP request, returning ``(status_code, json_body_or_None)``.

        Raises ``urllib.error.URLError`` only for transport-level failures; HTTP
        error statuses (4xx/5xx) are returned normally with their parsed body.
        """
        return _http_request(method, url, self.opts.auth_token, self.opts.user_agent,
                             self.opts.request_timeout)

    def _retry_after_ms(self, body: Optional[dict]) -> float:
        """Normalize a ``retry_after`` field to milliseconds.

        The userscript targets API v6, where ``retry_after`` is in milliseconds.
        On v8+ it is a float number of seconds, so convert.
        """
        ra = (body or {}).get("retry_after", 0) or 0
        try:
            ra = float(ra)
        except (TypeError, ValueError):
            ra = 0
        return ra * 1000.0 if self.opts.api_version >= 8 else ra

    def _search_url(self) -> str:
        o = self.opts
        if o.guild_id == "@me":
            base = f"{self._api_base}/channels/{o.channel_id}/messages/search"
            channel_param = None  # already scoped by the URL
        else:
            base = f"{self._api_base}/guilds/{o.guild_id}/messages/search"
            channel_param = o.channel_id

        params = [
            ("author_id", o.author_id or None),
            ("channel_id", channel_param or None),
            ("min_id", to_snowflake(o.min_id)),
            ("max_id", to_snowflake(o.max_id)),
            ("sort_by", "timestamp"),
            ("sort_order", "desc"),
            ("offset", self.offset),
            ("has", "link" if o.has_link else None),
            ("has", "file" if o.has_file else None),
            ("content", o.content or None),
            ("include_nsfw", "true" if o.include_nsfw else None),
        ]
        query = "&".join(
            f"{k}={urllib.parse.quote(str(v))}" for k, v in params if v is not None
        )
        return f"{base}?{query}"

    # ---- main loop ------------------------------------------------------- #

    def run(self) -> DeleteStats:
        o = self.opts
        # mutable run state (was a pile of `let`s in the userscript closure)
        self.delete_default = self._rand_delay()
        self.delete_delay = self.delete_default
        self.randomize_delay = True
        self.search_delay = self._rand_delay()
        self.del_count = 0
        self.archived_skip_count = 0
        self.fail_count = 0
        self.avg_ping = 0.0
        self.last_ping = 0
        self.grand_total: Optional[int] = None
        self.throttled_count = 0
        self.throttled_total_ms = 0.0
        self.offset = 0
        self.iterations = -1
        self.ended = False
        self.fail_in_row = 0
        self.success_in_row = 0
        self.archived_threads: set[str] = set()
        self.stopped = False
        self.aborted = False
        self._start = time.monotonic()

        self._log("success", f"Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._log(
            "debug",
            f'authorId="{o.author_id or ""}" guildId="{o.guild_id or ""}" '
            f'channelId="{o.channel_id or ""}" minId="{o.min_id or ""}" '
            f'maxId="{o.max_id or ""}" hasLink={o.has_link} hasFile={o.has_file}',
        )
        if o.dry_run:
            self._log("warn", "DRY RUN - searching only, no messages will be deleted.")
        self._progress(0, 1)

        while not self.ended:
            if not self._step():
                break

        return self._build_stats()

    def _build_stats(self) -> DeleteStats:
        return DeleteStats(
            deleted=self.del_count,
            failed=self.fail_count,
            archived_skipped=self.archived_skip_count,
            throttled_count=self.throttled_count,
            throttled_total_ms=self.throttled_total_ms,
            grand_total=self.grand_total or 0,
            elapsed_ms=(time.monotonic() - self._start) * 1000,
            stopped=self.stopped,
            aborted=self.aborted,
        )

    def _end(self, message: Optional[str] = None, *, level: str = "success") -> bool:
        if not self.ended:
            if message:
                self._log(level, message)
            self._log(
                "success",
                f"Ended at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}! "
                f"Total time: {ms_to_hms((time.monotonic() - self._start) * 1000)}",
            )
            self._log(
                "verb",
                f"Rate limited: {self.throttled_count} times. "
                f"Total time throttled: {ms_to_hms(self.throttled_total_ms)}.",
            )
            self._log("debug", f"Deleted {self.del_count} messages, {self.fail_count} failed.")
            self.ended = True
        return False  # returning False tells run()'s loop to stop

    def _is_run_complete(self) -> bool:
        return self.grand_total is not None and (self.del_count + self.fail_count) >= self.grand_total

    # One iteration: search a page, then delete what's on it. Returns True to
    # keep looping, False to stop.
    def _step(self) -> bool:
        if self._should_stop():
            self.stopped = True
            return self._end("Stopped by you!", level="error")

        # --- search request (with transient-network retry) ---------------- #
        url = self._search_url()
        status, body = None, None
        for attempt in range(self.opts.network_retries):
            try:
                t0 = time.monotonic()
                status, body = self._request("GET", url)
                self.last_ping = int((time.monotonic() - t0) * 1000)
                self.avg_ping = (self.avg_ping * 0.9 + self.last_ping * 0.1) if self.avg_ping > 0 else self.last_ping
                break
            except urllib.error.URLError as err:
                if attempt + 1 >= self.opts.network_retries:
                    self._log("error", "Search request failed (network):", str(err))
                    return self._end()
                self._log("warn", f"Search network error, retrying in 5s ({attempt + 1}/{self.opts.network_retries})...")
                self._wait(5000)

        # 202: channel not indexed yet
        if status == 202:
            w = self._retry_after_ms(body)
            self.throttled_count += 1
            self.throttled_total_ms += w
            self._log("warn", f"This channel wasn't indexed, waiting {w:.0f}ms for Discord to index it...")
            self._wait(w)
            return True

        if status == 429:
            w = self._retry_after_ms(body)
            self.throttled_count += 1
            self.throttled_total_ms += w
            self.search_delay = w * 1.1
            self._log("warn", f"Discord said don't search for {w:.0f}ms!")
            self._print_delay_stats()
            self._wait(self.search_delay)
            return True

        if status is None or status >= 400 or body is None:
            self._log("error", f"Error searching messages, API responded with status {status}!", body)
            return self._end()

        # --- parse search results ----------------------------------------- #
        total = body.get("total_results", 0)
        if self.grand_total is None:
            self.grand_total = total

        discovered = []
        for convo in body.get("messages", []):
            hit = next((m for m in convo if m.get("hit")), None)
            if hit:
                discovered.append(hit)

        to_delete = [
            m for m in discovered
            if (m.get("type") in DELETABLE_TYPES) or (m.get("pinned") and self.opts.include_pinned)
        ]
        to_delete = [m for m in to_delete if m.get("channel_id") not in self.archived_threads]

        delete_ids = {m["id"] for m in to_delete}
        skipped = [m for m in discovered if m["id"] not in delete_ids]
        self.fail_count += len(skipped)
        archived_count = sum(1 for m in skipped if m.get("channel_id") in self.archived_threads)
        system_count = len(skipped) - archived_count
        self.archived_skip_count += archived_count
        if skipped:
            self._progress(self.del_count, self.grand_total or 1, True)

        deletable_messages = (self.grand_total or 0) - self.archived_skip_count
        etr = ms_to_hms(
            self.search_delay * round(deletable_messages / 25)
            + (self.delete_delay + self.avg_ping) * deletable_messages
        )
        self._log(
            "info",
            f"Total messages found: {total}",
            f"(Hits: {len(body.get('messages', []))}, Delete: {len(to_delete)}, "
            f"Skipped: {len(skipped)} (system {system_count})) offset: {self.offset}",
        )
        self._print_delay_stats()
        self._log("verb", f"Estimated time remaining: {etr}")

        # --- dry run: enumerate, never delete ----------------------------- #
        if self.opts.dry_run:
            for m in to_delete:
                self._log_planned(m)
            self.del_count += len(to_delete)
            self._progress(self.del_count, self.grand_total or 1)
            self.offset += len(discovered) if discovered else 25
            if self.offset >= total:
                return self._end()
            self._wait(self.search_delay)
            return True

        # --- real deletion ------------------------------------------------ #
        if to_delete:
            self.iterations += 1
            if self.iterations < 1 and self._confirm is not None:
                if not self._confirm(total, etr, to_delete):
                    self.aborted = True
                    return self._end("Aborted by you!", level="error")

            self._delete_batch(to_delete)
            if self.ended:  # stopped/aborted mid-batch
                return False

            if skipped:
                self.offset += len(skipped)
                self._log("verb", f"Found {len(skipped)} undeletable messages. Increasing offset to {self.offset}.")

            if self._is_run_complete():
                return self._end()

            # reset delays for the next page
            self.delete_default = self._rand_delay()
            self.delete_delay = self.delete_default
            self.search_delay = self._rand_delay()
            self.randomize_delay = True
            self._log("verb", f"Searching next messages in {self.search_delay:.0f}ms...",
                      f"(offset: {self.offset})" if self.offset else "")
            self._wait(self.search_delay)
            return True

        # --- nothing deletable on this page ------------------------------- #
        if skipped:
            self._log("verb",
                      f"No deletables on this page ({system_count} system, {archived_count} archived). "
                      f"Advancing offset by {len(skipped)}.")
            self.offset += len(skipped)
            if self._is_run_complete() or self.offset >= total:
                return self._end()
            self._wait(self.search_delay)
            return True
        if total - self.offset > 0:
            self._log("warn", "API returned an empty page. Searching next page.")
            self.offset += 25
            self._wait(self.search_delay)
            return True
        return self._end()

    def _log_planned(self, message: dict) -> None:
        ts = self._fmt_ts(message.get("timestamp"))
        if message.get("attachments"):
            preview = "[ATTACHMENTS]"
        else:
            preview = (message.get("content") or "").replace("\n", " ")
        self._log("verb", f"[dry-run] would delete ({ts}): {preview}")

    @staticmethod
    def _fmt_ts(timestamp: Optional[str]) -> str:
        if not timestamp:
            return "?"
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            return str(timestamp)

    def _delete_batch(self, messages: list) -> None:
        n = len(messages)
        i = 0
        while i < n:
            message = messages[i]
            channel_id = message.get("channel_id")

            if channel_id in self.archived_threads:
                self._log("verb", f"Skipping message in archived thread {channel_id}")
                i += 1
                continue
            if self._should_stop():
                self.stopped = True
                self._end("Stopped by you!", level="error")
                return

            processed = self.del_count + self.fail_count
            pct = (processed + 1) / (self.grand_total or 1) * 100
            self._log(
                "verb",
                f"{pct:.2f}% ({processed + 1}/{self.grand_total}) | DEL ({self._fmt_ts(message.get('timestamp'))}): "
                f"{(message.get('content') or '').replace(chr(10), ' ')}",
            )

            advance = True
            try:
                t0 = time.monotonic()
                url = f"{self._api_base}/channels/{channel_id}/messages/{message['id']}"
                status, body = self._request("DELETE", url)
                self.last_ping = int((time.monotonic() - t0) * 1000)
                self.avg_ping = self.avg_ping * 0.9 + self.last_ping * 0.1
            except urllib.error.URLError as err:
                self._log("error", "Delete request failed (network):", str(err))
                self.fail_count += 1
                if i < n - 1:
                    self._wait(self.delete_delay)
                i += 1
                continue

            if status is not None and 200 <= status < 300:
                self._on_delete_success()
            else:
                advance = self._on_delete_failure(status, body, message, channel_id)
                if self.ended:  # archived-mark path uses continue, never sets ended;
                    return       # but a stop during retry-wait could end the run

            if i < n - 1:
                self._wait(self.delete_delay)
            if advance:
                i += 1

    def _on_delete_success(self) -> None:
        self.fail_in_row = 0
        self.success_in_row += 1
        self.del_count += 1
        self._progress(self.del_count, self.grand_total or 1)
        if self.randomize_delay:
            self.delete_default = self._rand_delay()
            self.delete_delay = self.delete_default
        if self.success_in_row > 4 and self.delete_delay > self.delete_default and not self.randomize_delay:
            self.delete_delay *= 0.94812
            self._log("debug", f"Lowering delay to {self.delete_delay:.0f}ms")
        elif self.delete_delay < self.delete_default:
            self.delete_default = self._rand_delay()
            self.delete_delay = self.delete_default
            self.randomize_delay = True
            self._log("debug", f"Default delay, {self.delete_default}.")

    def _on_delete_failure(self, status, body, message, channel_id) -> bool:
        """Handle a non-2xx delete. Returns True to advance to the next message,
        False to retry the same one."""
        err = body
        self.fail_in_row += 1
        self.success_in_row = 0
        self.randomize_delay = False

        code = (err or {}).get("code")
        err_msg = (err or {}).get("message", "") or ""
        archived = (
            (status == 400 and code == 50083)
            or (status == 403 and "archiv" in err_msg.lower())
            or (status == 404 and "archiv" in err_msg.lower())
        )

        if archived:
            self._log(
                "warn",
                f"Archived thread detected (status {status}{', code ' + str(code) if code else ''}), "
                f"marking channel {channel_id} as archived",
            )
            self.archived_threads.add(channel_id)
            # Faithful to the userscript: don't count it here. The message stays
            # in the result set and is re-discovered (and counted as skipped) on
            # the next search, which also advances the offset past it.
            return True

        if status == 429:
            w = self._retry_after_ms(err)
            self._log("warn", f"Failed to delete - Discord said go away for {w:.0f}ms!")
            self.throttled_count += 1
            self.throttled_total_ms += w
            multi = 1.632
            if w * 1.532 > self.delete_delay:
                self.delete_delay = w * multi
            else:
                self.delete_delay *= 0.94812
                if self.delete_delay < w:
                    self.delete_delay = w * multi
                self._log("warn", "Delete delay is already greater than wait time. Reducing instead.")
            self._print_delay_stats()
            self._wait(self.delete_delay)
            return False  # retry this same message

        self._log("error", f"Error deleting message, API responded with status {status}!", err)
        self.fail_count += 1
        return True


def delete_messages(
    opts: DeleteOptions,
    logger: Optional[Logger] = None,
    stop_check: Optional[StopCheck] = None,
    confirm: Optional[ConfirmFn] = None,
    on_progress: Optional[ProgressFn] = None,
) -> DeleteStats:
    """Convenience wrapper around :class:`MessageDeleter`."""
    return MessageDeleter(opts, logger, stop_check, confirm, on_progress).run()
