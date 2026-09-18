"""Configuration repositories: the ports and their SQLAlchemy implementations (spec 013).

The package re-exports nothing. Importing **any** of its modules loads the rule schema, and
with it pydantic, pandas and the indicator catalog, because ``StoredRule`` carries a parsed
``Rule``: that cost is deliberate and confined to this package, so ``Base``, the engine, the
models and the ``Database`` handle stay importable by the Telegram and API layers without the
analysis stack (decision D99). ``protocols.py`` holds the three ports, ``tickers.py``,
``rules.py`` and ``assignments.py`` their implementations over a ``Session``; ``rules.py`` owns
the document codec. No implementation commits, rolls back or closes: the unit of work belongs to
the caller, which opens exactly one ``Database.session()`` per unit of work.
"""
