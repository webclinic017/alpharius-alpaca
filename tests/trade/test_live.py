import itertools
import os
import time

import alpaca.trading as trading
import pandas as pd
import pytest
import sqlalchemy

import alpharius.trade as trade
from ..fakes import Account, FakeTradingClient, FakeProcessor, FakeProcessorFactory, FakeDbEngine, FakeDataClient


@pytest.fixture(autouse=True)
def mock_time(mocker):
    mocker.patch.object(time, 'sleep')
    mocker.patch.object(time, 'time', side_effect=itertools.count(1615987700))


@pytest.fixture(autouse=True)
def mock_engine(mocker):
    engine = FakeDbEngine()
    mocker.patch.object(sqlalchemy, 'create_engine', return_value=engine)
    return engine


@pytest.mark.parametrize("trading_frequency",
                         [trade.TradingFrequency.FIVE_MIN,
                          trade.TradingFrequency.CLOSE_TO_CLOSE,
                          trade.TradingFrequency.CLOSE_TO_OPEN])
def test_run_success(mock_trading_client, trading_frequency):
    fake_processor_factory = FakeProcessorFactory(trading_frequency)
    fake_processor = fake_processor_factory.processor
    trading = trade.Live(processor_factories=[fake_processor_factory], data_client=FakeDataClient())

    trading.run()

    assert mock_trading_client.get_order_call_count > 0
    assert mock_trading_client.get_all_positions_call_count > 0
    assert mock_trading_client.submit_order_call_count > 0
    assert mock_trading_client.get_account_call_count > 0
    assert fake_processor.get_stock_universe_call_count > 0
    assert fake_processor.process_data_call_count > 0


def test_run_with_processors(mock_trading_client):
    trading = trade.Live(processor_factories=trade.PROCESSOR_FACTORIES, data_client=FakeDataClient())

    trading.run()

    assert mock_trading_client.get_account_call_count > 0


def test_not_run_on_market_close_day(mocker, mock_trading_client):
    data_client = FakeDataClient()
    trading = trade.Live(processor_factories=[], data_client=data_client)
    mocker.patch.object(FakeTradingClient, 'get_calendar', return_value=[])

    trading.run()

    assert mock_trading_client.get_account_call_count > 0
    assert data_client.get_data_call_count == 0


def test_not_run_if_far_from_market_open(mocker, mock_trading_client):
    data_client = FakeDataClient()
    trading = trade.Live(processor_factories=[], data_client=data_client)
    mocker.patch.object(time, 'time',
                        return_value=mock_trading_client.get_clock().next_open.timestamp() - 4000)

    trading.run()

    assert mock_trading_client.get_account_call_count > 0
    assert data_client.get_data_call_count == 0


def test_small_position_not_open(mocker, mock_trading_client):
    fake_processor_factory = FakeProcessorFactory(
        trade.TradingFrequency.CLOSE_TO_OPEN)
    trading = trade.Live(processor_factories=[fake_processor_factory], data_client=FakeDataClient())
    mocker.patch.object(FakeTradingClient, 'get_account',
                        return_value=Account('2000', '0.1', '8000'))

    trading.run()

    assert mock_trading_client.submit_order_call_count == 0


def test_trade_transactions_executed(mocker):
    trading = trade.Live(processor_factories=[], data_client=FakeDataClient())
    expected_transactions = [
        {'symbol': 'A', 'action_type': trade.ActionType.BUY_TO_OPEN,
         'qty': None, 'side': 'buy', 'notional': 900},
        {'symbol': 'B', 'action_type': trade.ActionType.SELL_TO_OPEN,
         'qty': 9, 'side': 'sell', 'notional': None},
        {'symbol': 'QQQ', 'action_type': trade.ActionType.SELL_TO_CLOSE,
         'qty': 10, 'side': 'sell'},
        {'symbol': 'GOOG', 'action_type': trade.ActionType.BUY_TO_CLOSE,
         'qty': 10, 'side': 'buy'},
    ]
    actions = [trade.Action(t['symbol'], t['action_type'], 1, 100,
                            FakeProcessor(trade.TradingFrequency.FIVE_MIN))
               for t in expected_transactions]
    mock_place_order = mocker.patch.object(trade.Live, '_place_order')

    trading._trade(actions)

    for t in expected_transactions:
        t.pop('action_type')
        mock_place_order.assert_any_call(**t)


def test_trade_transactions_skipped(mock_trading_client):
    trading = trade.Live(processor_factories=[], data_client=FakeDataClient())
    actions = [trade.Action('QQQ', trade.ActionType.BUY_TO_CLOSE, 1, 100,
                            FakeProcessor(trade.TradingFrequency.FIVE_MIN)),
               trade.Action('GOOG', trade.ActionType.SELL_TO_CLOSE, 1, 100,
                            FakeProcessor(trade.TradingFrequency.FIVE_MIN)),
               trade.Action('AAPL', trade.ActionType.SELL_TO_CLOSE, 1, 100,
                            FakeProcessor(trade.TradingFrequency.FIVE_MIN))]

    trading._trade(actions)

    assert mock_trading_client.submit_order_call_count == 0


def test_update_db(mocker, mock_engine):
    exit_time = pd.to_datetime('2022-11-04 05:35:00-0400')
    mocker.patch.object(os.path, 'isdir', return_value=True)
    mocker.patch.object(os, 'listdir', return_value=['trading.txt'])
    mocker.patch('builtins.open', mocker.mock_open(read_data='data'))
    mocker.patch.object(time, 'time', return_value=exit_time.timestamp() + 30)
    trading = trade.Live(processor_factories=[], data_client=FakeDataClient())
    trading._update_db([trade.Action('QQQ', trade.ActionType.SELL_TO_CLOSE, 1, 100,
                                     FakeProcessor(trade.TradingFrequency.FIVE_MIN))])

    assert mock_engine.conn.execute.call_count == 3
