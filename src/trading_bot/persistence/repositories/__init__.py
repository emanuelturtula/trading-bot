"""Repositories: the ports and their SQLAlchemy implementations (specs 013 and 014).

The package re-exports nothing. ``protocols.py`` holds the five ports; ``tickers.py``,
``rules.py``, ``assignments.py``, ``signals.py`` and ``bot_state.py`` their implementations over
a ``Session``, and ``rules.py`` owns the rule-document codec while ``signals.py`` owns the
indicator-values one.

Importing ``protocols.py`` or any of the three configuration implementations loads the rule
schema, and with it pydantic, pandas and the indicator catalog, because ``StoredRule`` carries a
parsed ``Rule``: that cost is deliberate and confined to those modules, so ``Base``, the engine,
the models and the ``Database`` handle stay importable by the Telegram and API layers without
the analysis stack (decision D99). ``signals.py`` and ``bot_state.py`` deliberately do **not**
load it, so the engine can record a signal and report a run without it (decision D120).

No implementation commits, rolls back or closes: the unit of work belongs to the caller, which
opens exactly one ``Database.session()`` per unit of work (and never a second one at the same
time in one thread, spec 014 D110).
"""
