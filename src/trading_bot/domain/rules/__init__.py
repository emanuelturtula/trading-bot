"""Pure rule model: the JSON schema of a rule, its validation and its canonical form.

``schema.py`` and ``json_schema.py`` are the only modules of ``domain/`` allowed to import
pydantic (spec 006, AC22); ``errors.py`` imports neither pydantic nor the indicator catalog, so
the API can map error kinds without loading TA-Lib. Rules are declarative data validated against
a whitelist: nothing here evaluates code (CLAUDE.md rule 8). Import from the submodules
(``trading_bot.domain.rules.schema``, ``trading_bot.domain.rules.errors``, ...): this package
re-exports nothing. Design and decisions: spec 006.
"""
