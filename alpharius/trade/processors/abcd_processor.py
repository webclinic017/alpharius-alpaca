import datetime
from typing import List, Optional

import numpy as np
from ..common import (
    ActionType, Context, DataSource, Processor, ProcessorFactory, TradingFrequency,
    Position, ProcessorAction, Mode, DAYS_IN_A_MONTH, DATETIME_TYPE)
from ..stock_universe import IntradayVolatilityStockUniverse

NUM_UNIVERSE_SYMBOLS = 20


class AbcdProcessor(Processor):

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
        if context.symbol in self._positions:
            return self._close_position(context)
        else:
            return self._open_position(context)

    def _get_l2h(self, context: Context) -> float:
        key = context.symbol + context.current_time.strftime('%F')
        if key in self._memo:
            return self._memo[key]
        interday_highs = context.interday_lookback['High'][-DAYS_IN_A_MONTH:]
        interday_lows = context.interday_lookback['Low'][-DAYS_IN_A_MONTH:]
        l2h_gains = [h / l - 1 for h, l in zip(interday_highs, interday_lows)]
        l2h_avg = np.average(l2h_gains)
        self._memo[key] = l2h_avg
        return l2h_avg

    def _open_position(self, context: Context) -> Optional[ProcessorAction]:
        t = context.current_time.time()
        if t < datetime.time(10, 30) or t > datetime.time(15, 30):
            return
        if abs(context.current_price / context.prev_day_close - 1) > 0.5:
            return
        market_open_index = context.market_open_index
        if market_open_index is None:
            return
        open_price = context.intraday_lookback['Open'][market_open_index]
        intraday_closes = context.intraday_lookback['Close'][market_open_index:]
        if len(intraday_closes) < 10:
            return
        intraday_high = np.max(intraday_closes)
        if not context.prev_day_close < open_price < context.current_price < intraday_high:
            return
        l2h = self._get_l2h(context)
        if intraday_high / open_price - 1 < 1 * l2h:
            return
        if intraday_high / context.current_price - 1 < 0.6 * l2h:
            return
        max_i = np.argmax(intraday_closes)
        if len(intraday_closes) > max_i + 15 or len(intraday_closes) < max_i + 10:
            return
        if context.mode == Mode.TRADE:
            self._logger.debug(f'[{context.current_time.strftime("%F %H:%M")}] [{context.symbol}] '
                               f'Checking bar size conditions. Current price: {context.current_price}.')
        if abs(intraday_closes[-3] - intraday_closes[-2]) < abs(intraday_closes[-2] - intraday_closes[-1]):
            return
        if intraday_closes[-1] >= intraday_closes[-2]:
            return
        self._logger.debug(f'[{context.current_time.strftime("%F %H:%M")}] [{context.symbol}] '
                           f'L2h: {l2h}. Intraday high: {intraday_high}. Current price: {context.current_price}.')
        self._positions[context.symbol] = {'entry_time': context.current_time,
                                           'status': 'active'}
        return ProcessorAction(context.symbol, ActionType.BUY_TO_OPEN, 1)

    def _close_position(self, context: Context) -> Optional[ProcessorAction]:
        position = self._positions[context.symbol]
        if position['status'] != 'active':
            return
        intraday_closes = context.intraday_lookback['Close']
        take_profit = (context.current_time == position['entry_time'] + datetime.timedelta(minutes=30)
                       and len(intraday_closes) >= 7
                       and context.current_price > intraday_closes[-7])
        is_close = (take_profit or
                    context.current_time >= position['entry_time'] + datetime.timedelta(minutes=40)
                    or context.current_time.time() >= datetime.time(16, 0))
        self._logger.debug(f'[{context.current_time.strftime("%F %H:%M")}] [{context.symbol}] '
                           f'Closing position: {is_close}. Current price {context.current_price}.')
        if is_close:
            position['status'] = 'inactive'
            return ProcessorAction(context.symbol, ActionType.SELL_TO_CLOSE, 1)


class AbcdProcessorFactory(ProcessorFactory):

    def __init__(self):
        super().__init__()

    def create(self,
               lookback_start_date: DATETIME_TYPE,
               lookback_end_date: DATETIME_TYPE,
               data_source: DataSource,
               output_dir: str,
               *args, **kwargs) -> AbcdProcessor:
        return AbcdProcessor(lookback_start_date, lookback_end_date, data_source, output_dir)
