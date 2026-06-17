"""Command-line interface for undiscord-cli.

Configuration precedence (highest wins):
    command-line flags  >  environment variables  >  config file  >  defaults
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import urllib.error
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .deleter import (
    DeleteOptions,
    DeleteStats,
    DiscordAPIError,
    MessageDeleter,
    fetch_dm_channels,
    fetch_guilds,
    ms_to_hms,
)

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

_ANSI = {
    "": "",
    "debug": "\033[90m",   # grey
    "info": "\033[36m",    # cyan
    "verb": "\033[90m",    # grey
    "warn": "\033[33m",    # yellow
    "error": "\033[31m",   # red
    "success": "\033[32m",  # green
}
_RESET = "\033[0m"

# Hidden unless --verbose. Real-time delete lines and the time estimate use
# "verb", which now shows by default; "debug" stays for internal diagnostics.
_VERBOSE_ONLY = {"debug"}


class ConsoleLogger:
    def __init__(self, *, verbose: bool, quiet: bool, color: bool, redact: bool):
        self.verbose = verbose
        self.quiet = quiet
        self.color = color
        self.redact = redact
        # Message content is arbitrary Unicode (Korean, emoji, ...). Force the
        # console to UTF-8 so it displays correctly and never crashes a delete
        # run on a legacy code-page console (e.g. cp949 / cp1252).
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    def __call__(self, level: str, *args: object) -> None:
        if self.quiet and level not in ("error", "success"):
            return
        if level in _VERBOSE_ONLY and not self.verbose:
            return
        parts = []
        for a in args:
            if a is None:
                continue
            parts.append(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False, default=str))
        msg = "\t".join(p for p in parts if p != "")
        if self.redact:
            msg = self._redact(msg)
        if self.color and level in _ANSI and _ANSI[level]:
            msg = f"{_ANSI[level]}{msg}{_RESET}"
        stream = sys.stderr if level in ("warn", "error") else sys.stdout
        try:
            print(msg, file=stream, flush=True)
        except UnicodeEncodeError:
            # Safety net if the stream couldn't be switched to UTF-8.
            enc = getattr(stream, "encoding", None) or "utf-8"
            print(msg.encode(enc, "replace").decode(enc, "replace"), file=stream, flush=True)

    @staticmethod
    def _redact(msg: str) -> str:
        # Best-effort: blank out anything inside a DEL/dry-run preview after the colon.
        for marker in ("DEL (", "would delete ("):
            idx = msg.find(marker)
            if idx != -1:
                colon = msg.find("): ", idx)
                if colon != -1:
                    return msg[: colon + 3] + "REDACTED"
        return msg


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

def _load_config(path: Optional[Path]) -> dict:
    if path is None:
        # implicit ./config.toml if present
        implicit = Path("config.toml")
        if implicit.is_file():
            path = implicit
        else:
            return {}
    if not path.is_file():
        raise SystemExit(f"error: config file not found: {path}")
    if tomllib is None:
        raise SystemExit("error: TOML config requires Python 3.11+ (tomllib).")
    # Tolerate a UTF-8 BOM (common when files are saved by Windows editors),
    # which tomllib would otherwise reject.
    text = path.read_text(encoding="utf-8-sig")
    return tomllib.loads(text)


def _read_channels_from_index(path: Path) -> list[str]:
    """Read channel ids from a Discord data-export ``messages/index.json``.

    That file maps ``{ "<channel_id>": "<name>", ... }`` so the keys are the ids.
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SystemExit(f"error: {path} is not a channel index object")
    return [str(k) for k in data.keys()]


def _split_channels(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = str(raw).split(",")
    return [c.strip() for c in items if str(c).strip()]


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="undiscord-cli",
        description="Mass-delete your own Discord messages from the command line. "
                    "A CLI port of gen3vra's deletediscordmessages userscript.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WARNING: Automating a user account (using a user token) violates Discord's\n"
            "Terms of Service and can get your account terminated. Use only on your own\n"
            "account and your own messages, at your own risk.\n\n"
            "Examples:\n"
            "  undiscord-cli --guild-id @me --channel-id 123 --author-id 456 --dry-run\n"
            "  undiscord-cli --config config.toml --yes\n"
            "  undiscord-cli --guild-id 111 --channel-id 222,333 --content hello\n"
        ),
    )
    p.add_argument("--config", type=Path, help="path to a TOML config file (default: ./config.toml if present)")

    auth = p.add_argument_group("authentication")
    auth.add_argument("--token", help="Discord user auth token (or set env DISCORD_TOKEN)")
    auth.add_argument("--token-file", type=Path, help="read the auth token from this file")
    auth.add_argument("--author-id", help="only delete messages by this user id")

    loc = p.add_argument_group("location")
    loc.add_argument("--guild-id", help='server (guild) id, or "@me" for DMs / group DMs')
    loc.add_argument("--channel-id", action="append", default=None,
                     help="channel id(s); repeatable or comma-separated. "
                          "Omit for a server to search ALL its channels (required for DMs).")
    loc.add_argument("--import-index", type=Path,
                     help="import channel ids from a Discord data-export messages/index.json")
    loc.add_argument("--all-guilds", action="store_true",
                     help="sweep EVERY server you're in (pair with --author-id to wipe only your messages)")
    loc.add_argument("--all-dms", action="store_true",
                     help="sweep every open DM / group DM")
    loc.add_argument("--all", action="store_true",
                     help="shorthand for --all-guilds --all-dms (everything, everywhere)")

    rng = p.add_argument_group("range filters")
    rng.add_argument("--min-id", help="only messages after this id (After)")
    rng.add_argument("--max-id", help="only messages before this id (Before)")
    rng.add_argument("--min-date", help="only messages after this date, e.g. 2023-01-31T00:00")
    rng.add_argument("--max-date", help="only messages before this date")

    flt = p.add_argument_group("search filters")
    flt.add_argument("--content", help="only messages containing this text")
    flt.add_argument("--has-link", action="store_true", default=None, help="only messages with a link")
    flt.add_argument("--has-file", action="store_true", default=None, help="only messages with a file")
    flt.add_argument("--include-nsfw", action="store_true", default=None, help="search NSFW channels too")
    flt.add_argument("--include-pinned", action="store_true", default=None, help="also delete pinned messages")

    beh = p.add_argument_group("behavior")
    beh.add_argument("--dry-run", action="store_true", help="search and report only; delete nothing")
    beh.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    beh.add_argument("--api-version", type=int, default=None, help="Discord API version (default: 9)")
    beh.add_argument("--user-agent", help="override the HTTP User-Agent")
    beh.add_argument("--delay-min", type=int, default=None, help="min delay between deletes, ms (default 1000)")
    beh.add_argument("--delay-max", type=int, default=None, help="max delay between deletes, ms (default 2000)")

    out = p.add_argument_group("output")
    out.add_argument("-v", "--verbose", action="store_true", help="show verbose/debug logging")
    out.add_argument("-q", "--quiet", action="store_true", help="only errors and final summary")
    out.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    out.add_argument("--redact", action="store_true", help="hide message content in logs")

    p.add_argument("--version", action="version", version=f"undiscord-cli {__version__}")
    return p


def _resolve_token(args, cfg: dict) -> Optional[str]:
    if args.token:
        return args.token.strip()
    if args.token_file:
        return Path(args.token_file).read_text(encoding="utf-8-sig").strip()
    if os.environ.get("DISCORD_TOKEN"):
        return os.environ["DISCORD_TOKEN"].strip()
    if cfg.get("token"):
        return str(cfg["token"]).strip()
    if cfg.get("token_file"):
        return Path(cfg["token_file"]).read_text(encoding="utf-8-sig").strip()
    return None


def _pick(cli_value, cfg, key, default=None):
    """CLI value wins when set (not None); otherwise config; otherwise default."""
    if cli_value is not None:
        return cli_value
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return default


def _make_confirm(args):
    if args.yes or args.dry_run:
        return None

    def confirm(total: int, etr: str, preview: list) -> bool:
        print(f"\nDo you want to delete ~{total} messages?  Estimated time: {etr}", file=sys.stderr)
        print("---- preview ----", file=sys.stderr)
        for m in preview[:10]:
            author = m.get("author", {})
            who = f"{author.get('username', '?')}"
            disc = author.get("discriminator")
            if disc and disc != "0":
                who += f"#{disc}"
            body = "[ATTACHMENTS]" if m.get("attachments") else (m.get("content") or "")
            body = body.replace("\n", " ")
            print(f"  {who}: {body}", file=sys.stderr)
        if len(preview) > 10:
            print(f"  ... and more", file=sys.stderr)
        sys.stderr.write("Proceed? [y/N] ")
        sys.stderr.flush()
        try:
            answer = input().lstrip(chr(0xFEFF)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    return confirm


def _dm_label(c: dict) -> str:
    cid = c.get("id", "?")
    recipients = c.get("recipients") or []
    names = ", ".join(r.get("username", "?") for r in recipients) or "?"
    if c.get("type") == 3:  # group DM
        return f"group DM '{c.get('name') or names}' ({cid})"
    return f"DM with {names} ({cid})"


def _confirm_sweep(n_guilds: int, n_dms: int) -> bool:
    """Strong upfront confirmation before a multi-target sweep."""
    where = []
    if n_guilds:
        where.append(f"{n_guilds} server(s)")
    if n_dms:
        where.append(f"{n_dms} DM channel(s)")
    # Write the whole prompt (including the input line) to stderr and flush, so
    # it's always visible and on one stream — input()'s own prompt would go to
    # stdout and can appear out of order or get lost when output is redirected.
    sys.stderr.write(f"\n*** SWEEP MODE *** About to scan {' and '.join(where)} and DELETE matching messages.\n")
    sys.stderr.write("This is irreversible. Tip: --dry-run to preview first, or --yes to skip this prompt.\n")
    sys.stderr.write(">>> Type DELETE (capital letters) and press Enter to proceed, or Ctrl+C to cancel: ")
    sys.stderr.flush()
    try:
        # lstrip a BOM that some Windows shells prepend when piping input.
        return input().lstrip(chr(0xFEFF)).strip() == "DELETE"
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: Optional[list[str]] = None) -> int:
    # Make console output robust: a single non-encodable character in a message
    # (emoji, unusual symbols) must not crash the run on a legacy code-page console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    cfg = _load_config(args.config)

    token = _resolve_token(args, cfg)
    if not token:
        print("error: no auth token. Provide --token, --token-file, env DISCORD_TOKEN, or token in config.",
              file=sys.stderr)
        return 2

    color = sys.stdout.isatty() and not args.no_color and os.environ.get("NO_COLOR") is None
    logger = ConsoleLogger(
        verbose=args.verbose,
        quiet=args.quiet,
        color=color,
        redact=args.redact or bool(cfg.get("redact")),
    )

    api_version = int(_pick(args.api_version, cfg, "api_version", 9))
    user_agent = _pick(args.user_agent, cfg, "user_agent")
    author_id = _pick(args.author_id, cfg, "author_id")

    sweep_guilds = args.all or args.all_guilds
    sweep_dms = args.all or args.all_dms
    sweep = sweep_guilds or sweep_dms

    # Build the list of targets to process: each a dict(guild_id, channel_id, label).
    jobs: list = []
    if sweep:
        fetch_kw = {"api_version": api_version}
        if user_agent:
            fetch_kw["user_agent"] = user_agent
        try:
            if sweep_guilds:
                guilds = fetch_guilds(token, **fetch_kw)
                logger("info", f"Found {len(guilds)} server(s).")
                for g in guilds:
                    jobs.append({"guild_id": str(g["id"]), "channel_id": None,
                                 "label": f"server '{g.get('name', '?')}' ({g['id']})"})
            if sweep_dms:
                dms = fetch_dm_channels(token, **fetch_kw)
                logger("info", f"Found {len(dms)} open DM channel(s).")
                for c in dms:
                    jobs.append({"guild_id": "@me", "channel_id": str(c["id"]), "label": _dm_label(c)})
        except DiscordAPIError as e:
            logger("error", f"Could not enumerate targets: {e}")
            if getattr(e, "status", None) == 401:
                logger("error", "(401 Unauthorized - check that your token is valid.)")
            return 2
        except urllib.error.URLError as e:
            logger("error", f"Network error while enumerating targets: {e}")
            return 2
        if sweep_guilds and not author_id:
            logger("warn", "No --author-id set: in servers you can only delete YOUR OWN messages "
                           "(others' deletes will fail). Add --author-id <your id> to target just yours.")
    else:
        guild_id = _pick(args.guild_id, cfg, "guild_id")
        if not guild_id:
            logger("error", 'No guild id. Provide --guild-id (use "@me" for DMs), or --all/--all-guilds/--all-dms.')
            return 2
        # resolve channels: --import-index > --channel-id > config
        channels: list = []
        if args.import_index:
            channels = _read_channels_from_index(args.import_index)
        elif args.channel_id:
            for entry in args.channel_id:
                channels.extend(_split_channels(entry))
        else:
            channels = _split_channels(cfg.get("channel_id"))
        if not channels:
            if str(guild_id) == "@me":
                logger("error", "DMs require a channel id. Provide --channel-id, --import-index, or use --all-dms.")
                return 2
            # A real guild with no channel -> search the WHOLE guild (every channel).
            channels = [None]
        for ch in channels:
            label = f"channel {ch}" if ch else f"entire guild {guild_id} (all channels)"
            jobs.append({"guild_id": str(guild_id), "channel_id": ch, "label": label})

    if not jobs:
        logger("error", "Nothing to do - no targets resolved.")
        return 1

    base_opts = dict(
        auth_token=token,
        author_id=author_id,
        min_id=_pick(args.min_id, cfg, "min_id") or _pick(args.min_date, cfg, "min_date"),
        max_id=_pick(args.max_id, cfg, "max_id") or _pick(args.max_date, cfg, "max_date"),
        content=_pick(args.content, cfg, "content"),
        has_link=bool(_pick(args.has_link, cfg, "has_link", False)),
        has_file=bool(_pick(args.has_file, cfg, "has_file", False)),
        include_nsfw=bool(_pick(args.include_nsfw, cfg, "include_nsfw", False)),
        include_pinned=bool(_pick(args.include_pinned, cfg, "include_pinned", False)),
        dry_run=args.dry_run,
        api_version=api_version,
        delay_min_ms=int(_pick(args.delay_min, cfg, "delay_min", 1000)),
        delay_max_ms=int(_pick(args.delay_max, cfg, "delay_max", 2000)),
    )
    if user_agent:
        base_opts["user_agent"] = user_agent

    # graceful stop on Ctrl+C
    stop_flag = {"stop": False}

    def _sigint(_signum, _frame):
        if stop_flag["stop"]:
            print("\nForce quit.", file=sys.stderr)
            raise KeyboardInterrupt
        stop_flag["stop"] = True
        print("\nStopping after the current request... (Ctrl+C again to force quit)", file=sys.stderr)

    signal.signal(signal.SIGINT, _sigint)

    confirm = _make_confirm(args)
    if sweep:
        # One strong confirmation up front instead of prompting per target.
        if confirm is not None:  # interactive (not --yes / not --dry-run)
            n_g = sum(1 for j in jobs if j["guild_id"] != "@me")
            n_d = len(jobs) - n_g
            if not _confirm_sweep(n_g, n_d):
                logger("error", "Aborted.")
                return 1
        confirm = None

    totals = DeleteStats()
    for idx, job in enumerate(jobs):
        if stop_flag["stop"]:
            break
        if len(jobs) > 1:
            logger("info", f"=== [{idx + 1}/{len(jobs)}] {job['label']} ===")
        elif job["channel_id"] is None and job["guild_id"] != "@me":
            logger("info", f"No channel specified - searching the ENTIRE guild {job['guild_id']} (all channels).")
        opts = DeleteOptions(guild_id=job["guild_id"], channel_id=job["channel_id"], **base_opts)
        deleter = MessageDeleter(
            opts,
            logger=logger,
            stop_check=lambda: stop_flag["stop"],
            confirm=confirm,
            on_progress=None,
        )
        try:
            stats = deleter.run()
        except KeyboardInterrupt:
            stop_flag["stop"] = True
            stats = deleter._build_stats()
        totals.deleted += stats.deleted
        totals.failed += stats.failed
        totals.archived_skipped += stats.archived_skipped
        totals.throttled_count += stats.throttled_count
        totals.throttled_total_ms += stats.throttled_total_ms

    verb = "Would delete" if args.dry_run else "Deleted"
    logger("success",
           f"\nAll done. {verb} {totals.deleted} message(s), {totals.failed} failed/skipped "
           f"across {len(jobs)} target(s). "
           f"Rate limited {totals.throttled_count} time(s) ({ms_to_hms(totals.throttled_total_ms)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
