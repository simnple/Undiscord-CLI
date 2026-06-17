"""undiscord-cli — mass-delete your own Discord messages from the command line.

A CLI port of gen3vra's ``deletediscordmessages`` userscript
(https://github.com/gen3vra/deletediscordmessages), itself based on
victornpb's undiscord. Zero third-party dependencies (standard library only).
"""

from .deleter import (
    DeleteOptions,
    DeleteStats,
    DiscordAPIError,
    MessageDeleter,
    delete_messages,
    fetch_dm_channels,
    fetch_guilds,
    to_snowflake,
    ms_to_hms,
)

__version__ = "0.2.0"

__all__ = [
    "DeleteOptions",
    "DeleteStats",
    "DiscordAPIError",
    "MessageDeleter",
    "delete_messages",
    "fetch_dm_channels",
    "fetch_guilds",
    "to_snowflake",
    "ms_to_hms",
    "__version__",
]
