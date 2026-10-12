"""Managing the bot from Telegram (spec 018; issues #19, #20).

- ``unit_of_work``: the repositories a command reads and writes, one session per command;
- ``commands``: parsing and handling of ``/add``, ``/remove``, ``/list``, ``/rules``,
  ``/status``, ``/pause``, ``/resume`` and ``/help``. Library-free and never sees an
  unauthorized chat;
- ``poller``: the ``getUpdates`` loop, the allowlist check and the replies; the only module of
  the package that imports python-telegram-bot.

No command places an order or touches a broker (CLAUDE.md rule 1). This package re-exports
nothing.
"""
