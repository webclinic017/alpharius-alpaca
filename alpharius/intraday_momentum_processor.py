from .common import *
from .stock_universe import TopVolumeUniverse, IntradayRangeStockUniverse
from typing import List
import datetime
import numpy as np

NUM_UNIVERSE_SYMBOLS = 100
NUM_DIRECTIONAL_SYMBOLS = 5
ENTRY_TIME = datetime.time(10, 0)
EXIT_TIME = datetime.time(15, 30)


class IntradayMomentumProcessor(Processor):

    def __init__(self,
                 lookback_start_date: DATETIME_TYPE,
                 lookback_end_date: DATETIME_TYPE,
                 data_source: DataSource,
                 output_dir: str) -> None:
        super().__init__()
        self._positions = dict()
        self._stock_universe = TopVolumeUniverse(lookback_start_date,
                                                 lookback_end_date,
                                                 data_source,
                                                 num_stocks=NUM_UNIVERSE_SYMBOLS)
        self._output_dir = output_dir
        self._logger = logging_config(os.path.join(self._output_dir, 'intraday_momentum_processor.txt'),
                                      detail=True,
                                      name='intraday_momentum_processor')

    def get_trading_frequency(self) -> TradingFrequency:
        return TradingFrequency.FIVE_MIN

    def get_stock_universe(self, view_time: DATETIME_TYPE) -> List[str]:
        return list(set(self._stock_universe.get_stock_universe(view_time) +
                        list(self._positions.keys())))

    def setup(self, hold_positions: List[Position]) -> None:
        self._positions = {
            position.symbol: {
                'side': 'long' if position.qty > 0 else 'short',
                'entry_time': position.entry_time,
                'entry_price': position.entry_price,
            }
            for position in hold_positions
        }

    def process_data(self, context: Context) -> Optional[Action]:
        if context.symbol in self._positions:
            return self._close_position(context)
        else:
            return self._open_position(context)

    def _open_position(self, context: Context):
        if context.current_time.time() > datetime.time(15, 30) or context.current_time.time() < datetime.time(10, 0):
            return

        intraday_closes = context.intraday_lookback['Close']
        if len(intraday_closes) < 6:
            return
        volumes = context.intraday_lookback['Volume']

        # interday_close = context.interday_lookback['Close'][-DAYS_IN_A_MONTH:]
        # interday_change = [interday_close[i + 1] / interday_close[i] - 1 for i in range(len(interday_close) - 1)]
        # std = np.std(interday_change)
        # if abs(context.current_price / intraday_closes[-2] - 1) < 0.5 * std:
        #     return

        intraday_change_abs = [abs(intraday_closes[i + 1] / intraday_closes[i] - 1)
                               for i in range(len(intraday_closes) - 1)]
        if intraday_change_abs[-1] < 3 * np.average(intraday_change_abs[:-1]):
            return

        if volumes[-1] < 2 * np.max(volumes[:-1]):
            return

        if intraday_closes[-1] > intraday_closes[-2]:
            self._positions[context.symbol] = {
                'side': 'long', 'entry_time': context.current_time, 'entry_price': context.current_price}
            return Action(context.symbol, ActionType.BUY_TO_OPEN, 1, context.current_price)
        # else:
        #     self._positions[context.symbol] = {
        #         'side': 'short', 'entry_time': context.current_time, 'entry_price': context.current_price}
        #     return Action(context.symbol, ActionType.SELL_TO_OPEN, 1, context.current_price)

    def _close_position(self, context: Context):
        action = None
        symbol = context.symbol
        side = self._positions[symbol]['side']
        entry_price = self._positions[symbol]['entry_price']
        entry_time = self._positions[symbol]['entry_time']
        force_close = context.current_time - entry_time >= datetime.timedelta(minutes=30)
        if side == 'long':
            if force_close or context.current_price > entry_price:
                action = Action(context.symbol, ActionType.SELL_TO_CLOSE, 1, context.current_price)
        else:
            if force_close or context.current_price < entry_price:
                action = Action(context.symbol, ActionType.BUY_TO_CLOSE, 1, context.current_price)
        if action is not None:
            self._positions.pop(symbol)
        return action


class IntradayMomentumProcessorFactory(ProcessorFactory):

    def __init__(self):
        super().__init__()

    def create(self,
               lookback_start_date: DATETIME_TYPE,
               lookback_end_date: DATETIME_TYPE,
               data_source: DataSource,
               output_dir: str,
               *args, **kwargs) -> IntradayMomentumProcessor:
        return IntradayMomentumProcessor(lookback_start_date, lookback_end_date, data_source, output_dir)
