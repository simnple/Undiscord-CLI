# Undiscord-CLI

> Mass-delete **your own** Discord messages from the command line.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

A command-line port of [gen3vra's `deletediscordmessages`](https://github.com/gen3vra/deletediscordmessages)
userscript (which itself improves on [victornpb's *undiscord*](https://github.com/victornpb/deleteDiscordMessages)).
It reproduces the same search → delete loop, randomized delays, rate-limit
backoff (HTTP `202` "not indexed" and `429` "too fast"), and archived-thread /
system-message handling — but driven by flags and a config file instead of a
browser UI.

- **Zero dependencies** — pure Python standard library (Python **3.11+**).
- Servers, single channels, DMs / group DMs, or **everything at once** (`--all`).
- `--dry-run` + a confirmation prompt so you see what *would* be deleted first.
- Filters: author, date/id range, text content, has-link, has-file, NSFW, pinned.
- Real-time progress with an estimated time remaining.

> [!WARNING]
> **Automating a user account (using a *user token*) is against Discord's
> [Terms of Service](https://discord.com/terms) and can get your account
> terminated.** This tool is for deleting *your own* messages on *your own*
> account, and you use it entirely at your own risk. Never share your token, and
> never run a token that isn't yours.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Examples](#examples)
- [Options](#options)
- [Scope: server vs channel vs DMs](#scope-server-vs-channel-vs-dms)
- [Output & progress](#output--progress)
- [Getting a token and ids](#getting-a-token-and-ids)
- [How it works](#how-it-works)
- [Credits](#credits)
- [License](#license)

## Install

No install needed — clone and run as a module:

```powershell
git clone https://github.com/<you>/Undiscord-CLI.git
cd Undiscord-CLI
python -m undiscord --help
```

Or install it to get the `undiscord-cli` command on your PATH:

```powershell
pip install .
undiscord-cli --help
```

## Quick start

1. Get your auth token and ids (see [Getting a token and ids](#getting-a-token-and-ids)).
2. Do a **dry run** first to preview what matches — this deletes nothing:

   ```powershell
   $env:DISCORD_TOKEN = "your-token"
   python -m undiscord --guild-id "@me" --channel-id 1111 --author-id 2222 --dry-run
   ```

3. When it looks right, drop `--dry-run`. You'll be asked to confirm before anything is deleted:

   ```powershell
   python -m undiscord --guild-id "@me" --channel-id 1111 --author-id 2222
   ```

Stop a run at any time with **Ctrl+C** — it stops after the current request and
prints a summary. Press it twice to force-quit.

## Configuration

Precedence (highest wins): **command-line flags → environment variables → config file → defaults**.

- **Token**: `--token`, `--token-file`, the `DISCORD_TOKEN` env var, or `token` / `token_file` in the config.
- **Config file**: pass `--config path.toml`, or drop a `config.toml` in the working directory (auto-loaded).

Copy [`config.example.toml`](config.example.toml) to `config.toml` and edit it:

```powershell
copy config.example.toml config.toml
python -m undiscord --config config.toml --dry-run
```

> [!IMPORTANT]
> Keep your token out of version control — `config.toml`, `token.txt` and
> `*.token` are already in [`.gitignore`](.gitignore).

## Examples

```powershell
# Your messages in a specific server channel, containing "lol"
python -m undiscord --guild-id 111 --channel-id 222 --author-id 333 --content lol

# Several channels at once (repeat the flag or comma-separate)
python -m undiscord --guild-id 111 --channel-id 222,333 --channel-id 444

# Whole server: omit --channel-id to search EVERY channel in a guild
python -m undiscord --guild-id 111 --author-id 333 --dry-run

# Only messages with attachments, in a date range
python -m undiscord --guild-id 111 --channel-id 222 --has-file `
    --min-date 2022-01-01T00:00 --max-date 2023-01-01T00:00

# Every DM channel from a Discord data export (messages/index.json)
python -m undiscord --guild-id "@me" --import-index ".\package\messages\index.json"

# Sweep EVERY server you're in (only your own messages) — preview first!
python -m undiscord --all-guilds --author-id 333 --dry-run

# Sweep every open DM / group DM
python -m undiscord --all-dms --dry-run

# Everything, everywhere (all servers + all DMs)
python -m undiscord --all --author-id 333 --dry-run

# Skip the prompt for unattended runs
python -m undiscord --config config.toml --yes
```

## Options

Run `python -m undiscord --help` for the full list. Highlights:

| Flag | Meaning |
| --- | --- |
| `--guild-id` | Server id, or `@me` for DMs / group DMs (omit only with `--all*`) |
| `--channel-id` | Channel id(s); repeatable or comma-separated. Omit on a server to hit all its channels |
| `--all-guilds` | Sweep every server you're in (pair with `--author-id`) |
| `--all-dms` | Sweep every open DM / group DM |
| `--all` | Shorthand for `--all-guilds --all-dms` |
| `--author-id` | Only delete messages by this user id |
| `--min-id` / `--max-id` | Snowflake id range (After / Before) |
| `--min-date` / `--max-date` | Date range, e.g. `2023-01-31T00:00` |
| `--content` | Only messages containing this text |
| `--has-link` / `--has-file` | Only messages with a link / file |
| `--include-nsfw` | Search NSFW channels too |
| `--include-pinned` | Also delete pinned messages |
| `--dry-run` | Search and report only; delete nothing |
| `-y, --yes` | Skip the confirmation prompt |
| `--delay-min` / `--delay-max` | Delay between deletes, ms (default 1000–2000) |
| `--api-version` | Discord API version (default 9) |
| `-v, --verbose` / `-q, --quiet` | More / less logging |
| `--redact` | Hide message content in logs (for screenshots) |

## Scope: server vs channel vs DMs

- **One channel** — pass `--guild-id <server>` and `--channel-id <id>`.
- **Whole server** — pass `--guild-id <server>` and *omit* `--channel-id`. Discord's
  guild search returns matches from every channel, and deletes go to each
  message's own channel. Combine with `--author-id` to remove only your messages.
- **DMs / group DMs** — use `--guild-id "@me"` with a specific `--channel-id`.
- **All servers** — `--all-guilds` enumerates every server you're in (via
  `/users/@me/guilds`) and runs a whole-server pass on each. Pair with
  `--author-id <you>` so you only target your own messages.
- **All DMs** — `--all-dms` enumerates your *open* DM and group-DM channels (via
  `/users/@me/channels`). Closed conversations won't appear — reach those with
  `--import-index path/to/messages/index.json` from a Discord data export.
- **Everything** — `--all` is shorthand for `--all-guilds --all-dms`.

Sweep modes (`--all*`) ask for one upfront confirmation (type `DELETE`) unless
you pass `--yes`. **Always `--dry-run` first** — these touch a lot at once and
deletion is irreversible.

## Output & progress

By default (no flags) the tool prints, in real time:

- the per-page summary (`Total messages found: ... (Delete: N, Skipped: ...)`),
- an **estimated time remaining** (`Estimated time remaining: 1h 2m 3s`), recomputed each page,
- a line for **every message as it's deleted**: `33.33% (1/3) | DEL (2023-05-02 14:30:00): <content>`.

Use `-v, --verbose` for internal diagnostics (delay stats, backoff adjustments),
`-q, --quiet` for only errors and the final summary, and `--redact` to blank out
message content in the delete lines (handy for screenshots). `--dry-run` lists
what *would* be deleted using the same lines, prefixed with `[dry-run]`.

### A note on rate-limit timing

The original userscript targets Discord API **v6**, where the `retry_after`
field is in **milliseconds**. On v8+ it is in **seconds**, so this tool detects
the configured `--api-version` and normalizes the value to milliseconds. The
backoff math (`× 1.632` on a 429, randomized 1–2s base delay) is otherwise
preserved from the userscript. The default API version here is `9`; pass
`--api-version 6` to match the userscript exactly.

## Getting a token and ids

The browser userscript scrapes these for you; on the CLI you supply them:

- **Auth token** — DevTools → Network tab → any request to `discord.com/api` →
  copy the `authorization` request header.
  ([help](https://github.com/victornpb/deleteDiscordMessages/blob/master/help/authToken.md))
- **Author id** — your own user id (enable Developer Mode → right-click your
  avatar → *Copy User ID*).
- **Guild / channel id** — open the channel in the web app; the URL is
  `discord.com/channels/<guildId>/<channelId>`. For DMs the guild part is `@me`.
  ([help](https://github.com/victornpb/deleteDiscordMessages/blob/master/help/channelId.md))

## How it works

- [`undiscord/deleter.py`](undiscord/deleter.py) is a faithful port of the
  userscript's `deleteMessages` routine. The recursive `recurse()` is rewritten
  as an explicit loop so large deletions can't overflow the Python call stack.
- The browser-only pieces (the popover UI, and token/author scraping via
  `localStorage` and webpack) are replaced by CLI flags + a config file.
- [`undiscord/cli.py`](undiscord/cli.py) handles argument parsing, config
  merging, logging, the confirmation prompt, Ctrl+C handling, target
  enumeration (`--all*`), and looping over multiple targets.
- Tests live in [`tests/`](tests/) and run offline (no network):
  `python -m pytest` or `python tests/test_deleter.py`.

## Credits

- [gen3vra/deletediscordmessages](https://github.com/gen3vra/deletediscordmessages) — the userscript this CLI is ported from.
- [victornpb/deleteDiscordMessages](https://github.com/victornpb/deleteDiscordMessages) — the original *undiscord* project.

## License

[MIT](LICENSE). `userscript.js` in this repo is the upstream source, kept for reference.
