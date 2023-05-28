import datetime
from typing import List, Optional

import numpy as np
from ..common import (
    ActionType, Context, DataSource, Processor, ProcessorFactory, TradingFrequency,
    Position, ProcessorAction, Mode, DAYS_IN_A_MONTH, DATETIME_TYPE)
from ..stock_universe import IntradayVolatilityStockUniverse

NUM_UNIVERSE_SYMBOLS = 20
N = 4


class DownFourProcessor(Processor):

    def __init__(self,
                 lookback_start_date: DATETIME_TYPE,
                 lookback_end_date: DATETIME_TYPE,
                 data_source: DataSource,
                 output_dir: str) -> None:
        super().__init__(output_dir)
        self._positions = dict()
        self._stock_universe = IntradayVolatilityStockUniverse(lookback_start_date,
                                                               lookback_end_date,
                                                               data_source,
                                                               num_stocks=NUM_UNIVERSE_SYMBOLS)
        self._memo = dict()

    def get_trading_frequency(self) -> TradingFrequency:
        return TradingFrequency.FIVE_MIN

    def setup(self, hold_positions: List[Position], current_time: Optional[DATETIME_TYPE]) -> None:
        to_remove = [symbol for symbol, position in self._positions.items()
                     if position['status'] != 'active']
        for symbol in to_remove:
            self._positions.pop(symbol)
        self._memo = dict()

    def get_stock_universe(self, view_time: DATETIME_TYPE) -> List[str]:
        return list(set(self._stock_universe.get_stock_universe(view_time) +
                        list(self._positions.keys())))

    def process_data(self, context: Context) -> Optional[ProcessorAction]:
        if self.is_active(context.symbol):
            return self._close_position(context)
        elif context.symbol not in self._positions:
            return self._open_position(context)

    def _get_h2l(self, context: Context) -> float:
        key = context.symbol + context.current_time.strftime('%F')
        if key in self._memo:
            h2l = self._memo[key]
        else:
            interday_highs = context.interday_lookback['High'][-DAYS_IN_A_MONTH:]
            interday_lows = context.interday_lookback['Low'][-DAYS_IN_A_MONTH:]
            h2l_losses = [l / h - 1 for h, l in zip(interday_highs, interday_lows)]
            h2l = np.average(h2l_losses)
            self._memo[key] = h2l
        return h2l

    def _open_position(self, context: Context) -> Optional[ProcessorAction]:
        t = context.current_time.time()
        if datetime.time(13, 0) <= t < datetime.time(14, 0) or t >= datetime.time(15, 0):
            return
        market_open_index = context.market_open_index
        if market_open_index is None:
            return
        intraday_closes = context.intraday_lookback['Close'][market_open_index:]
        if len(intraday_closes) < N:
            return
        if abs(context.current_price / context.prev_day_close - 1) > 0.5:
            return
        intraday_opens = context.intraday_lookback['Open'][market_open_index:]
        if intraday_opens[-N] > context.prev_day_close > intraday_closes[-1]:
            return
        losses = [intraday_closes[i] / intraday_opens[i] - 1 for i in range(-N, 0)]
        for lose in losses:
            if lose > 0:
                return
        h2l = self._get_h2l(context)
        is_trade = losses[-2] < 0.3 * h2l and losses[-1] > 0.05 * h2l
        if is_trade or (context.mode == Mode.TRADE and losses[-2] < 0.25 * h2l):
            self._logger.debug(f'[{context.current_time.strftime("%F %H:%M")}] [{context.symbol}] '
                               f'Prev loss: {losses[-2] * 100:.2f}%. '
                               f'Current loss: {losses[-1] * 100:.2f}%. '
                               f'H2l: {h2l * 100:.2f}%. Current price {context.current_price}.')
        if is_trade:
            self._positions[context.symbol] = {'entry_time': context.current_time,
                                               'status': 'pending'}
            return ProcessorAction(context.symbol, ActionType.BUY_TO_OPEN, 1)

    def _close_position(self, context: Context) -> Optional[ProcessorAction]:
        position = self._positions[context.symbol]
        if position['status'] != 'active':
            return
        is_close = context.current_time >= position['entry_time'] + datetime.timedelta(minutes=20)
        self._logger.debug(f'[{context.current_time.strftime("%F %H:%M")}] [{context.symbol}] '
                           f'Closing position: {is_close}. Current price {context.current_price}.')
        if is_close:
            position['status'] = 'inactive'
            return ProcessorAction(context.symbol, ActionType.SELL_TO_CLOSE, 1)


class DownFourProcessorFactory(ProcessorFactory):

    def __init__(self):
        super().__init__()

    def create(self,
               lookback_start_date: DATETIME_TYPE,
               lookback_end_date: DATETIME_TYPE,
               data_source: DataSource,
               output_dir: str,
               *args, **kwargs) -> DownFourProcessor:
        return DownFourProcessor(lookback_start_date, lookback_end_date, data_source, output_dir)
