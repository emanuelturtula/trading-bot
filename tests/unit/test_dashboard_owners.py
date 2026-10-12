"""Each user sees and changes only their own tickers and rules (spec 024, AC3-AC6).

The administrator and an invited user share one running application; every check goes through
the dashboard like a browser would, and the data is read back per owner.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.dashboard import Browser, make_app
from tests.fixtures.repositories import sample_rule, sample_signal
from trading_bot.dashboard.unit_of_work import DashboardRepositories, sql_dashboard_unit_of_work
from trading_bot.data.tickers import AssetType, Exchange, TickerInfo
from trading_bot.domain.rules.schema import dump_rule
from trading_bot.domain.timeframe import Timeframe
from trading_bot.persistence.owner import ALL_OWNERS, Owner

ANA_PASSWORD = "-".join(["purple", "monkey", "dishwasher", "lamp"])
LINK = re.compile(r"/dashboard/invite/([A-Za-z0-9_-]+)</pre>")


class Users:
    """The administrator, an invited user ``ana`` and their ids."""

    def __init__(self, admin: Browser, ana: Browser, admin_id: int, ana_id: int) -> None:
        self.admin = admin
        self.ana = ana
        self.admin_id = admin_id
        self.ana_id = ana_id

    def work[T](self, owner: Owner, task: Callable[[DashboardRepositories], T]) -> T:
        unit_of_work = sql_dashboard_unit_of_work(self.admin.app.state.runtime.database)
        with unit_of_work(owner) as repositories:
            return task(repositories)

    def ticker(self, owner: int, symbol: str = "AAPL") -> int:
        return self.work(owner, lambda r: r.tickers.add(symbol, Timeframe.D1).id)

    def rule(self, owner: int, name: str) -> int:
        return self.work(owner, lambda r: r.rules.add(sample_rule(name), enabled=True).id)


@pytest.fixture
def users(tmp_path: Path) -> Iterator[Users]:
    with TestClient(make_app(tmp_path), follow_redirects=False) as client:
        admin = Browser(client)
        assert admin.login().status_code == 303
        page = admin.post("/dashboard/users", {"username": "ana"})
        match = LINK.search(page.text)
        assert match is not None
        ana = Browser(TestClient(admin.app, follow_redirects=False))
        accepted = ana.client.post(
            f"/dashboard/invite/{match.group(1)}",
            data={"password": ANA_PASSWORD, "repeat": ANA_PASSWORD},
        )
        assert accepted.status_code == 303
        ids = admin.repositories(
            lambda r: (r.users.get_by_username("admin"), r.users.get_by_username("ana"))
        )
        assert ids[0] is not None
        assert ids[1] is not None
        yield Users(admin, ana, ids[0].id, ids[1].id)


def test_each_user_lists_only_their_own_tickers_and_rules(users: Users) -> None:
    users.ticker(users.admin_id, "MSFT")
    users.rule(users.admin_id, "Admin rule")
    users.ticker(users.ana_id, "NVDA")
    users.rule(users.ana_id, "Ana rule")

    for browser, mine, theirs in (
        (users.admin, ("MSFT", "Admin rule"), ("NVDA", "Ana rule")),
        (users.ana, ("NVDA", "Ana rule"), ("MSFT", "Admin rule")),
    ):
        page = browser.get("/dashboard/tickers").text + browser.get("/dashboard/rules").text
        assert all(text in page for text in mine)
        assert not any(text in page for text in theirs)


def test_another_users_ticker_or_rule_is_not_found(users: Users) -> None:
    ticker = users.ticker(users.admin_id)
    rule = users.rule(users.admin_id, "Private")

    for path in (f"/dashboard/tickers/{ticker}", f"/dashboard/rules/{rule}"):
        assert users.ana.get(path).status_code == 404
    for path in (f"/dashboard/tickers/{ticker}/delete", f"/dashboard/rules/{rule}/delete"):
        users.ana.post(path)

    assert users.work(users.admin_id, lambda r: len(r.tickers.list_all())) == 1
    assert users.work(users.admin_id, lambda r: len(r.rules.list_all())) == 1


def test_two_users_may_watch_the_same_ticker_and_name_a_rule_alike(users: Users) -> None:
    users.ticker(users.admin_id)
    users.rule(users.admin_id, "Daily")
    users.admin.provider.add_ticker(
        TickerInfo(
            symbol="AAPL",
            name="Apple Inc.",
            asset_type=AssetType.EQUITY,
            exchange=Exchange.NASDAQ,
            currency="USD",
        )
    )

    added = users.ana.post("/dashboard/tickers", {"symbol": "AAPL", "timeframe": "1d"})
    created = users.ana.post(
        "/dashboard/rules", {"document": json.dumps(dump_rule(sample_rule("Daily")))}
    )

    assert added.status_code == 303
    assert created.status_code == 303
    assert users.work(ALL_OWNERS, lambda r: len(r.tickers.list_all())) == 2
    assert users.work(ALL_OWNERS, lambda r: len(r.rules.list_all())) == 2
    assert [t.user_id for t in users.work(users.ana_id, lambda r: r.tickers.list_all())] == [
        users.ana_id
    ]


def test_a_rule_of_another_user_cannot_be_assigned(users: Users) -> None:
    ticker = users.ticker(users.ana_id)
    foreign = users.rule(users.admin_id, "Admin rule")

    users.ana.post(f"/dashboard/tickers/{ticker}/rules", {"rule": str(foreign)})

    assert users.work(ALL_OWNERS, lambda r: r.assignments.list_all()) == ()


def test_only_an_administrator_pauses_the_bot(users: Users) -> None:
    assert "/dashboard/pause" not in users.ana.get("/dashboard/").text
    assert users.ana.post("/dashboard/pause").status_code == 403
    assert "/dashboard/pause" in users.admin.get("/dashboard/").text
    assert users.admin.post("/dashboard/pause").status_code == 303


def test_signals_and_charts_belong_to_their_owner(users: Users) -> None:
    """Spec 027, AC6: the history, the counts and the chart read only the user's own tickers."""
    admin_ticker = users.ticker(users.admin_id)
    admin_rule = users.rule(users.admin_id, "Mine")
    ana_ticker = users.ticker(users.ana_id, "MSFT")
    ana_rule = users.rule(users.ana_id, "Theirs")
    for owner, ticker_id, rule_id in (
        (users.admin_id, admin_ticker, admin_rule),
        (users.ana_id, ana_ticker, ana_rule),
    ):
        users.work(owner, lambda r, t=ticker_id, u=rule_id: r.assignments.assign(t, u))
        users.work(
            ALL_OWNERS,
            lambda r, t=ticker_id, u=rule_id: r.signals.record(
                sample_signal(r.tickers.get(t), r.rules.get(u))  # type: ignore[arg-type]
            ),
        )

    ana_page = users.ana.get("/dashboard/signals").text
    admin_page = users.admin.get("/dashboard/signals").text

    assert "<td>Theirs</td>" in ana_page
    assert "<td>Mine</td>" not in ana_page
    assert "<td>Mine</td>" in admin_page
    assert "<td>Theirs</td>" not in admin_page
    assert users.ana.get(f"/dashboard/tickers/{admin_ticker}/chart.json").status_code == 404
    assert users.ana.get(f"/dashboard/signals?ticker={admin_ticker}").text.count("<td>Mine") == 0
    assert users.work(users.ana_id, lambda r: r.signals.count()) == 1
    assert users.work(users.ana_id, lambda r: r.signals.history().items[0].rule_name) == "Theirs"
    assert users.work(ALL_OWNERS, lambda r: r.signals.count()) == 2
