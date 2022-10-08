import os

import alpaca_trade_api as tradeapi
import pytest
from alpharius.web import create_app
from .. import fakes


@pytest.fixture(autouse=True)
def mock_alpaca(mocker):
    client = fakes.FakeAlpaca()
    mocker.patch.object(tradeapi, 'REST', return_value=client)
    return client


@pytest.fixture(autouse=True)
def mock_cash_reserve():
    os.environ['CASH_RESERVE'] = '0'


@pytest.fixture
def app():
    app = create_app({'TESTING': True})
    return app


@pytest.fixture
def client(app):
    return app.test_client()

