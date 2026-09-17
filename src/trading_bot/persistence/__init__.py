"""SQLite persistence: engine and pragmas, sessions, metadata and Alembic migrations.

The package re-exports nothing: consumers import the module they need, so loading ``Base``
never drags Alembic or Mako in. ``engine.py`` builds the engine (WAL, foreign keys, busy
timeout, durable commits), ``database.py`` the ``Database`` handle returned by
``open_database``, ``base.py`` and ``models.py`` the declarative metadata, ``types.py`` the
UTC column type and ``migrator.py`` the Alembic entry points; the revisions themselves live in
``migrations/``, inside the package, so they ship in the wheel and in the image.
"""
