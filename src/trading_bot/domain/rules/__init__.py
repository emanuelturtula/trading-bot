"""Pure rules: the JSON schema of a rule, its validation, its canonical form and its evaluation.

``schema.py`` and ``json_schema.py`` are the only modules of ``domain/`` allowed to import
pydantic (spec 006, AC22); ``errors.py`` imports neither pydantic nor the indicator catalog, so
the API can map error kinds without loading TA-Lib. ``evaluator.py`` decides whether a parsed rule
fires on closed candles (``evaluate`` and ``evaluate_each``, spec 007): it consumes the models
without importing pydantic. Rules are declarative data validated against a whitelist: nothing
here evaluates code (CLAUDE.md rule 8). Import from the submodules
(``trading_bot.domain.rules.schema``, ``trading_bot.domain.rules.evaluator``, ...): this package
re-exports nothing. Design and decisions: specs 006 and 007.
"""
