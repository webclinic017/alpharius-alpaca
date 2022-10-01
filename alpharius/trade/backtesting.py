from .common import *
from .data import load_cached_daily_data, load_tradable_history, get_header
import alpaca_trade_api as tradeapi
from concurrent import futures
from typing import Any, Dict, List, Set, Tuple, Union
import collections
import datetime
import functools
import logging
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import os
import pandas as pd
import signal
import tabulate
import time

_MAX_WORKERS = 20


class Backtesting:

    def __init__(self,
                 start_date: Union[DATETIME_TYPE, str],
                 end_date: Union[DATETIME_TYPE, str],
                 processor_factories: List[ProcessorFactory]) -> None:
        if isinstance(start_date, str):
            start_date = pd.to_datetime(start_date)
        if isinstance(end_date, str):
            end_date = pd.to_datetime(end_date)
        self._start_date = start_date
        self._end_date = end_date
        self._processor_factories = processor_factories
        self._processors = []
        self._positions = []
        self._daily_equity = [1]
        self._num_win, self._num_lose = 0, 0
        self._cash = 1
        self._interday_data = None

        backtesting_output_dir = os.path.join(OUTPUT_DIR, 'backtesting')
        output_num = 1
        while True:
            output_dir = os.path.join(backtesting_output_dir,
                                      datetime.datetime.now().strftime('%m-%d'),
                                      f'{output_num:02d}')
            if not os.path.exists(output_dir):
                self._output_dir = output_dir
                os.makedirs(output_dir, exist_ok=True)
                break
            output_num += 1

        self._details_log = logging_config(os.path.join(self._output_dir, 'details.txt'), detail=False, name='details')
        self._summary_log = logging_config(os.path.join(self._output_dir, 'summary.txt'), detail=False, name='summary')

        alpaca = tradeapi.REST()
        calendar = alpaca.get_calendar(start=self._start_date.strftime('%F'),
                                       end=(self._end_date - datetime.timedelta(days=1)).strftime('%F'))
        self._market_dates = [market_day.date for market_day in calendar]
        signal.signal(signal.SIGINT, self._safe_exit)

        self._run_start_time = None
        self._interday_load_time = 0
        self._intraday_load_time = 0
        self._stock_universe_load_time = 0
        self._context_prep_time = 0
        self._data_process_time = 0

    def _safe_exit(self, signum, frame) -> None:
        logging.info('Signal [%d] received', signum)
        self._close()
        exit(1)

    def _close(self):
        self._print_profile()
        self._print_summary()
        self._plot_summary()
        for processor in self._processors:
            processor.teardown()

    def _init_processors(self, history_start) -> None:
        self._processors = []
        for factory in self._processor_factories:
            processor = factory.create(lookback_start_date=history_start,
                                       lookback_end_date=self._end_date,
                                       data_source=DEFAULT_DATA_SOURCE,
                                       output_dir=self._output_dir)
            self._processors.append(processor)

    def _take_snapshot(self):
        for factory in self._processor_factories:
            factory.snapshot(self._output_dir)

    def run(self) -> None:
        self._run_start_time = time.time()
        self._take_snapshot()
        history_start = self._start_date - datetime.timedelta(days=INTERDAY_LOOKBACK_LOAD)
        self._interday_data = load_tradable_history(history_start, self._end_date, DEFAULT_DATA_SOURCE)
        self._interday_load_time += time.time() - self._run_start_time
        self._init_processors(history_start)
        all_processor_c2c = np.all([processor.get_trading_frequency() == TradingFrequency.CLOSE_TO_CLOSE
                                    for processor in self._processors])
        for day in self._market_dates:
            if all_processor_c2c:
                self._process_c2c(day)
            else:
                self._process(day)
        self._close()

    def _load_stock_universe(self,
                             day: DATETIME_TYPE) -> Tuple[Dict[str, List[str]],
                                                          Dict[TradingFrequency, Set[str]]]:
        load_stock_universe_start = time.time()
        processor_stock_universes = dict()
        stock_universe = collections.defaultdict(set)
        for processor in self._processors:
            processor_name = type(processor).__name__
            processor_stock_universe = processor.get_stock_universe(day)
            processor_stock_universes[processor_name] = processor_stock_universe
            stock_universe[processor.get_trading_frequency()].update(processor_stock_universe)
        self._stock_universe_load_time += time.time() - load_stock_universe_start
        return processor_stock_universes, stock_universe

    def _process_data(self,
                      contexts: Dict[str, Context],
                      stock_universes: Dict[str, List[str]],
                      processors: List[Processor]) -> List[Action]:
        data_process_start = time.time()

        actions = []
        for processor in processors:
            processor_name = type(processor).__name__
            processor_stock_universe = stock_universes[processor_name]
            processor_contexts = []
            for symbol in processor_stock_universe:
                context = contexts.get(symbol)
                if context:
                    processor_contexts.append(context)
            actions.extend(processor.process_all_data(processor_contexts))

        self._data_process_time += time.time() - data_process_start
        return actions

    @functools.lru_cache()
    def _prepare_interday_lookback(self, day: DATETIME_TYPE, symbol: str) -> Optional[pd.DataFrame]:
        if symbol not in self._interday_data:
            return
        interday_data = self._interday_data[symbol]
        interday_ind = timestamp_to_index(interday_data.index, day)
        if interday_ind is None:
            return
        interday_lookback = interday_data.iloc[:interday_ind]
        return interday_lookback

    @staticmethod
    def _prepare_intraday_lookback(current_interval_start: DATETIME_TYPE, symbol: str,
                                   intraday_datas: Dict[str, pd.DataFrame]) -> Optional[pd.DataFrame]:
        intraday_data = intraday_datas[symbol]
        intraday_ind = timestamp_to_index(intraday_data.index, current_interval_start)
        if intraday_ind is None:
            return
        intraday_lookback = intraday_data.iloc[:intraday_ind + 1]
        return intraday_lookback

    @staticmethod
    def _adjust_split(intraday_lookback: pd.DataFrame, interday_lookback: pd.DataFrame) -> pd.DataFrame:
        prev_close = interday_lookback['Close'][-1]
        intraday_open = intraday_lookback['Open'][0]
        split_factor = np.round(intraday_open / prev_close)
        if split_factor > 1:
            intraday_lookback = intraday_lookback.apply(lambda x: x / split_factor)
            intraday_lookback['Volume'] = intraday_lookback['Volume'].apply(lambda x: x * split_factor ** 2)
        return intraday_lookback

    def _load_intraday_data(self,
                            day: DATETIME_TYPE,
                            stock_universe: Dict[TradingFrequency, Set[str]]) -> Dict[str, pd.DataFrame]:
        load_intraday_start = time.time()
        intraday_datas = dict()
        tasks = dict()
        unique_stock_universe = set()
        for _, symbols in stock_universe.items():
            unique_stock_universe.update(symbols)
        with futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            for symbol in unique_stock_universe:
                t = pool.submit(load_cached_daily_data, symbol, day, TimeInterval.FIVE_MIN, DEFAULT_DATA_SOURCE)
                tasks[symbol] = t
            for symbol, t in tasks.items():
                intraday_datas[symbol] = t.result()
        self._intraday_load_time += time.time() - load_intraday_start
        return intraday_datas

    def _process(self, day: DATETIME_TYPE) -> None:
        for processor in self._processors:
            processor.setup(self._positions, day)

        processor_stock_universes, stock_universe = self._load_stock_universe(day)

        intraday_datas = self._load_intraday_data(day, stock_universe)

        market_open = pd.to_datetime(pd.Timestamp.combine(day.date(), MARKET_OPEN)).tz_localize(TIME_ZONE)
        market_close = pd.to_datetime(pd.Timestamp.combine(day.date(), MARKET_CLOSE)).tz_localize(TIME_ZONE)
        current_interval_start = market_open

        executed_actions = []
        while current_interval_start < market_close:
            current_time = current_interval_start + datetime.timedelta(minutes=5)

            frequency_to_process = [TradingFrequency.FIVE_MIN]
            if current_interval_start == market_open:
                frequency_to_process = [TradingFrequency.FIVE_MIN,
                                        TradingFrequency.CLOSE_TO_OPEN]
            elif current_time == market_close:
                frequency_to_process = [TradingFrequency.FIVE_MIN,
                                        TradingFrequency.CLOSE_TO_OPEN,
                                        TradingFrequency.CLOSE_TO_CLOSE]

            prep_context_start = time.time()
            contexts = dict()
            unique_symbols = set()
            for frequency, symbols in stock_universe.items():
                if frequency in frequency_to_process:
                    unique_symbols.update(symbols)
            for symbol in unique_symbols:
                intraday_lookback = self._prepare_intraday_lookback(current_interval_start, symbol, intraday_datas)
                if intraday_lookback is None or len(intraday_lookback) == 0:
                    continue
                interday_lookback = self._prepare_interday_lookback(day, symbol)
                if interday_lookback is None or len(interday_lookback) == 0:
                    continue
                intraday_lookback = self._adjust_split(intraday_lookback, interday_lookback)
                current_price = intraday_lookback['Close'][-1]
                context = Context(symbol=symbol,
                                  current_time=current_time,
                                  current_price=current_price,
                                  interday_lookback=interday_lookback,
                                  intraday_lookback=intraday_lookback,
                                  mode=Mode.BACKTEST)
                contexts[symbol] = context
            self._context_prep_time += time.time() - prep_context_start

            processors = []
            for processor in self._processors:
                if processor.get_trading_frequency() in frequency_to_process:
                    processors.append(processor)
            actions = self._process_data(contexts, processor_stock_universes, processors)
            current_executed_actions = self._process_actions(current_time, actions)
            executed_actions.extend(current_executed_actions)

            current_interval_start += datetime.timedelta(minutes=5)

        for processor in self._processors:
            processor.teardown()

        self._log_day(day, executed_actions)

    def _process_c2c(self, day: DATETIME_TYPE) -> None:
        for processor in self._processors:
            processor.setup(self._positions, day)

        processor_stock_universes, stock_universe = self._load_stock_universe(day)
        market_close = pd.to_datetime(pd.Timestamp.combine(day.date(), MARKET_CLOSE)).tz_localize(TIME_ZONE)

        prep_context_start = time.time()
        contexts = {}
        for symbol in stock_universe[TradingFrequency.CLOSE_TO_CLOSE]:
            interday_lookback = self._prepare_interday_lookback(day, symbol)
            if interday_lookback is None:
                continue
            current_price = self._interday_data[symbol].loc[day]['Close']
            context = Context(symbol=symbol,
                              current_time=market_close,
                              current_price=current_price,
                              interday_lookback=interday_lookback,
                              intraday_lookback=None)
            contexts[symbol] = context
        self._context_prep_time += time.time() - prep_context_start

        actions = self._process_data(contexts, processor_stock_universes, self._processors)
        executed_actions = self._process_actions(market_close, actions)
        self._log_day(day, executed_actions)

    def _process_actions(self, current_time: DATETIME_TYPE, actions: List[Action]) -> List[List[Any]]:
        unique_actions = get_unique_actions(actions)

        close_actions = [action for action in unique_actions
                         if action.type in [ActionType.BUY_TO_CLOSE, ActionType.SELL_TO_CLOSE]]
        executed_closes = self._close_positions(current_time, close_actions)

        open_actions = [action for action in unique_actions
                        if action.type in [ActionType.BUY_TO_OPEN, ActionType.SELL_TO_OPEN]]
        self._open_positions(current_time, open_actions)

        return executed_closes

    def _pop_current_position(self, symbol: str) -> Optional[Position]:
        for ind, position in enumerate(self._positions):
            if position.symbol == symbol:
                current_position = self._positions.pop(ind)
                return current_position
        return None

    def _get_current_position(self, symbol: str) -> Optional[Position]:
        for position in self._positions:
            if position.symbol == symbol:
                return position
        return None

    def _close_positions(self, current_time: DATETIME_TYPE, actions: List[Action]) -> List[List[Any]]:
        executed_actions = []
        for action in actions:
            assert action.type in [ActionType.BUY_TO_CLOSE, ActionType.SELL_TO_CLOSE]
            symbol = action.symbol
            current_position = self._get_current_position(symbol)
            if current_position is None:
                continue
            if action.type == ActionType.BUY_TO_CLOSE and current_position.qty > 0:
                continue
            if action.type == ActionType.SELL_TO_CLOSE and current_position.qty < 0:
                continue
            self._pop_current_position(symbol)
            qty = current_position.qty * action.percent
            new_qty = current_position.qty - qty
            if abs(new_qty) > EPSILON:
                self._positions.append(Position(symbol, new_qty,
                                                current_position.entry_price,
                                                current_position.entry_time))
            spread_adjust = 1 - BID_ASK_SPREAD if action.type == ActionType.SELL_TO_CLOSE else 1 + BID_ASK_SPREAD
            adjusted_action_price = action.price * spread_adjust
            self._cash += adjusted_action_price * qty
            profit = adjusted_action_price / current_position.entry_price - 1
            if action.type == ActionType.BUY_TO_CLOSE:
                profit *= -1
            if profit > 0:
                self._num_win += 1
            else:
                self._num_lose += 1
            executed_actions.append([symbol, current_position.entry_time.time(), current_time.time(),
                                     'long' if action.type == ActionType.SELL_TO_CLOSE else 'short',
                                     qty, current_position.entry_price,
                                     action.price, f'{profit * 100:+.2f}%'])
        return executed_actions

    def _open_positions(self, current_time: DATETIME_TYPE, actions: List[Action]) -> None:
        tradable_cash = self._cash
        for position in self._positions:
            if position.qty < 0:
                tradable_cash += position.entry_price * position.qty * (1 + SHORT_RESERVE_RATIO)
        for action in actions:
            assert action.type in [ActionType.BUY_TO_OPEN, ActionType.SELL_TO_OPEN]
            symbol = action.symbol
            cash_to_trade = min(tradable_cash / len(actions), tradable_cash * action.percent)
            if abs(cash_to_trade) < EPSILON:
                cash_to_trade = 0
            qty = cash_to_trade / action.price
            if action.type == ActionType.SELL_TO_OPEN:
                qty = -qty
            old_position = self._get_current_position(symbol)
            if old_position is None:
                entry_price = action.price
                new_qty = qty
            else:
                self._pop_current_position(symbol)
                if old_position.qty == 0:
                    entry_price = action.price
                else:
                    entry_price = (old_position.entry_price * old_position.qty +
                                   action.price * qty) / (old_position.qty + qty)
                new_qty = qty + old_position.qty
            new_position = Position(symbol, new_qty, entry_price, current_time)
            self._positions.append(new_position)
            self._cash -= action.price * qty

    def _log_day(self,
                 day: DATETIME_TYPE,
                 executed_actions: List[List[Any]]) -> None:
        outputs = [get_header(day.date())]

        if executed_actions:
            trade_info = tabulate.tabulate(executed_actions,
                                           headers=['Symbol', 'Entry Time', 'Exit Time', 'Side', 'Qty',
                                                    'Entry Price', 'Exit Price', 'Gain/Loss'],
                                           tablefmt='grid')
            outputs.append('[ Trades ]')
            outputs.append(trade_info)

        if self._positions:
            position_info = []
            for position in self._positions:
                interday_data = self._interday_data[position.symbol]
                interday_ind = timestamp_to_index(interday_data.index, day)
                close_price, daily_change = None, None
                if interday_ind is not None:
                    close_price = interday_data['Close'][interday_ind]
                    if interday_ind > 0:
                        daily_change = (close_price / interday_data['Close'][interday_ind - 1] - 1) * 100
                change = (close_price / position.entry_price - 1) * 100 if close_price is not None else None
                value = close_price * position.qty if close_price is not None else None
                position_info.append([position.symbol, position.qty, position.entry_price,
                                      close_price,
                                      value,
                                      f'{daily_change:+.2f}%' if daily_change is not None else None,
                                      f'{change:+.2f}%' if change is not None else None])
            outputs.append('[ Positions ]')
            outputs.append(tabulate.tabulate(position_info,
                                             headers=['Symbol', 'Qty', 'Entry Price', 'Current Price',
                                                      'Current Value', 'Daily Change', 'Change'],
                                             tablefmt='grid'))

        equity = self._cash
        for position in self._positions:
            interday_data = self._interday_data[position.symbol]
            close_price = interday_data.loc[day]['Close'] if day in interday_data.index else position.entry_price
            equity += position.qty * close_price
        profit_pct = (equity / self._daily_equity[-1] - 1) * 100 if self._daily_equity[-1] else 0
        self._daily_equity.append(equity)
        total_profit_pct = ((equity / self._daily_equity[0] - 1) * 100)
        stats = [['Total Gain/Loss', f'{total_profit_pct:+.2f}%', 'Daily Gain/Loss', f'{profit_pct:+.2f}%']]

        outputs.append('[ Stats ]')
        outputs.append(tabulate.tabulate(stats, tablefmt='grid'))

        if not executed_actions and not self._positions:
            return
        self._details_log.info('\n'.join(outputs))

    def _print_summary(self) -> None:
        def _compute_risks(values: List[float],
                           m_values: List[float]) -> Tuple[Optional[float], Optional[float], float]:
            profits = [values[k + 1] / values[k] - 1 for k in range(len(values) - 1)]
            r = np.average(profits)
            std = np.std(profits)
            s = r / std * np.sqrt(252)
            a, b = None, None
            if len(values) == len(m_values):
                market_profits = [m_values[k + 1] / m_values[k] - 1 for k in range(len(m_values) - 1)]
                mr = np.average(market_profits)
                mvar = np.var(market_profits)
                b = np.cov(market_profits, profits, bias=True)[0, 1] / mvar
                a = (r - b * mr) * np.sqrt(252)
            return a, b, s

        def _compute_drawdown(values: List[float]) -> float:
            h = values[0]
            d = 0
            for v in values:
                d = min(v / h - 1, d)
                h = max(h, v)
            return d

        outputs = [get_header('Summary')]
        n_trades = self._num_win + self._num_lose
        success_rate = self._num_win / n_trades if n_trades > 0 else 0
        market_dates = self._market_dates[:len(self._daily_equity) - 1]
        if not market_dates:
            return
        summary = [['Time Range', f'{market_dates[0].date()} ~ {market_dates[-1].date()}'],
                   ['Success Rate', f'{success_rate * 100:.2f}%'],
                   ['Num of Trades', f'{n_trades} ({n_trades / len(market_dates):.2f} per day)']]
        outputs.append(tabulate.tabulate(summary, tablefmt='grid'))

        print_symbols = ['QQQ', 'SPY', 'TQQQ']
        market_symbol = 'SPY'
        stats = [['', 'My Portfolio'] + print_symbols]
        current_year = self._start_date.year
        current_start = 0
        for i, date in enumerate(market_dates):
            if i != len(market_dates) - 1 and market_dates[i + 1].year != current_year + 1:
                continue
            year_market_last_day_index = timestamp_to_index(self._interday_data[market_symbol].index, date)
            year_market_values = self._interday_data[market_symbol]['Close'][
                                 year_market_last_day_index - (i - current_start) - 1:year_market_last_day_index + 1]
            profit_pct = (self._daily_equity[i + 1] / self._daily_equity[current_start] - 1) * 100
            year_profit = [f'{current_year} Gain/Loss',
                           f'{profit_pct:+.2f}%']
            _, _, year_sharpe_ratio = _compute_risks(self._daily_equity[current_start: i + 2], year_market_values)
            year_sharpe = [f'{current_year} Sharpe Ratio',
                           f'{year_sharpe_ratio:.2f}']
            for symbol in print_symbols:
                if symbol not in self._interday_data:
                    continue
                last_day_index = timestamp_to_index(self._interday_data[symbol].index, date)
                symbol_values = list(self._interday_data[symbol]['Close'][
                                     last_day_index - (i - current_start) - 1:last_day_index + 1])
                symbol_profit_pct = (symbol_values[-1] / symbol_values[0] - 1) * 100
                _, _, symbol_sharpe = _compute_risks(symbol_values, year_market_values)
                year_profit.append(f'{symbol_profit_pct:+.2f}%')
                year_sharpe.append(f'{symbol_sharpe:.2f}')
            stats.append(year_profit)
            stats.append(year_sharpe)
            current_start = i
            current_year += 1
        total_profit_pct = (self._daily_equity[-1] / self._daily_equity[0] - 1) * 100
        total_profit = ['Total Gain/Loss', f'{total_profit_pct:+.2f}%']
        market_first_day_index = timestamp_to_index(
            self._interday_data[market_symbol].index, market_dates[0])
        market_last_day_index = timestamp_to_index(
            self._interday_data[market_symbol].index, market_dates[-1])
        market_values = self._interday_data[market_symbol]['Close'][
                        market_first_day_index - 1:market_last_day_index + 1]
        my_alpha, my_beta, my_sharpe_ratio = _compute_risks(self._daily_equity, market_values)
        my_drawdown = _compute_drawdown(self._daily_equity)
        alpha_row = ['Alpha', f'{my_alpha * 100:.2f}%']
        beta_row = ['Beta', f'{my_beta:.2f}']
        sharpe_ratio_row = ['Sharpe Ratio', f'{my_sharpe_ratio:.2f}']
        drawdown_row = ['Drawdown', f'{my_drawdown * 100:+.2f}%']
        for symbol in print_symbols:
            first_day_index = timestamp_to_index(self._interday_data[symbol].index, market_dates[0])
            last_day_index = timestamp_to_index(self._interday_data[symbol].index, market_dates[-1])
            symbol_values = self._interday_data[symbol]['Close'][first_day_index - 1:last_day_index + 1]
            symbol_total_profit_pct = (symbol_values[-1] / symbol_values[0] - 1) * 100
            total_profit.append(f'{symbol_total_profit_pct:+.2f}%')
            symbol_alpha, symbol_beta, symbol_sharpe_ratio = _compute_risks(symbol_values, market_values)
            alpha_row.append(f'{symbol_alpha * 100:.2f}%' if symbol_alpha is not None else None)
            beta_row.append(f'{symbol_beta:.2f}' if symbol_beta is not None else None)
            sharpe_ratio_row.append(f'{symbol_sharpe_ratio:.2f}')
            symbol_drawdown = _compute_drawdown(symbol_values)
            drawdown_row.append(f'{symbol_drawdown * 100:+.2f}%')
        stats.append(total_profit)
        stats.append(alpha_row)
        stats.append(beta_row)
        stats.append(sharpe_ratio_row)
        stats.append(drawdown_row)
        outputs.append(tabulate.tabulate(stats, tablefmt='grid'))
        self._summary_log.info('\n'.join(outputs))

    def _plot_summary(self) -> None:
        pd.plotting.register_matplotlib_converters()
        plot_symbols = ['QQQ', 'SPY', 'TQQQ']
        color_map = {'QQQ': '#78d237', 'SPY': '#FF6358', 'TQQQ': '#aa46be'}
        formatter = mdates.DateFormatter('%m-%d')
        current_year = self._start_date.year
        current_start = 0
        dates, values = [], [1]
        market_dates = self._market_dates[:len(self._daily_equity) - 1]
        for i, date in enumerate(market_dates):
            dates.append(date)
            values.append(self._daily_equity[i + 1] / self._daily_equity[current_start])
            if i != len(market_dates) - 1 and market_dates[i + 1].year != current_year + 1:
                continue
            dates = [dates[0] - datetime.timedelta(days=1)] + dates
            profit_pct = (self._daily_equity[i + 1] / self._daily_equity[current_start] - 1) * 100
            plt.figure(figsize=(10, 4))
            plt.plot(dates, values,
                     label=f'My Portfolio ({profit_pct:+.2f}%)',
                     color='#28b4c8')
            for symbol in plot_symbols:
                if symbol not in self._interday_data:
                    continue
                last_day_index = timestamp_to_index(self._interday_data[symbol].index, date)
                symbol_values = list(self._interday_data[symbol]['Close'][
                                     last_day_index + 1 - len(dates):last_day_index + 1])
                for j in range(len(symbol_values) - 1, -1, -1):
                    symbol_values[j] /= symbol_values[0]
                if symbol == 'TQQQ' and abs(symbol_values[-1] - 1) > 2 * abs(values[-1] - 1):
                    continue
                plt.plot(dates, symbol_values,
                         label=f'{symbol} ({(symbol_values[-1] - 1) * 100:+.2f}%)',
                         color=color_map[symbol])
            text_kwargs = {'family': 'monospace'}
            plt.xlabel('Date', **text_kwargs)
            plt.ylabel('Normalized Value', **text_kwargs)
            plt.title(f'{current_year} History', **text_kwargs, y=1.15)
            plt.grid(linestyle='--', alpha=0.5)
            plt.legend(ncol=len(plot_symbols) + 1, bbox_to_anchor=(0, 1),
                       loc='lower left', prop=text_kwargs)
            ax = plt.gca()
            ax.spines['right'].set_color('none')
            ax.spines['top'].set_color('none')
            ax.xaxis.set_major_formatter(formatter)
            plt.tight_layout()
            plt.savefig(os.path.join(self._output_dir, f'{current_year}.png'))
            plt.close()

            dates, values = [], [1]
            current_start = i
            current_year += 1

    def _print_profile(self):
        if self._run_start_time is None:
            return
        txt_output = os.path.join(self._output_dir, 'profile.txt')
        total_time = max(time.time() - self._run_start_time, 1E-7)
        outputs = [get_header('Profile')]
        profile = [
            ['Stage', 'Time Cost (s)', 'Percentage'],
            ['Total', f'{total_time:.0f}', '100%'],
            ['Interday Data Load', f'{self._interday_load_time:.0f}',
             f'{self._interday_load_time / total_time * 100:.0f}%'],
            ['Intraday Data Load', f'{self._intraday_load_time:.0f}',
             f'{self._intraday_load_time / total_time * 100:.0f}%'],
            ['Stock Universe Load', f'{self._stock_universe_load_time:.0f}',
             f'{self._stock_universe_load_time / total_time * 100:.0f}%'],
            ['Context Prepare', f'{self._context_prep_time:.0f}',
             f'{self._context_prep_time / total_time * 100:.0f}%'],
            ['Data Process', f'{self._data_process_time:.0f}',
             f'{self._data_process_time / total_time * 100:.0f}%'],
        ]
        outputs.append(tabulate.tabulate(profile, tablefmt='grid'))
        with open(txt_output, 'w') as f:
            f.write('\n'.join(outputs))
