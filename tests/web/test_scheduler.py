import os
import time
import threading

import pytest
from alpharius.web import scheduler
from .. import fakes


def test_trigger(client, mocker):
    thread = mocker.patch.object(threading, 'Thread')

    assert client.post('/trigger').status_code == 200
    thread.assert_called_once()


def test_trade_impl(mocker):
    mocker.patch('builtins.open', mocker.mock_open(read_data='data'))
    mocker.patch.object(os, 'makedirs')
    # Return empty calendar so that the trading does not run
    mock_get_calendar = mocker.patch.object(fakes.FakeAlpaca,
                                            'get_calendar',
                                            return_value=[])

    scheduler._trade_impl()

    mock_get_calendar.assert_called_once()


def test_backfill(mock_engine):
    scheduler.backfill()

    assert mock_engine.conn.execute.call_count > 0


@pytest.mark.parametrize('job_name',
                         ['trade', 'backfill'])
def test_scheduler(job_name):
    job = scheduler.scheduler.get_job(job_name)
    assert job.next_run_time.timestamp() < time.time() + 86400 * 3
