import argparse
import trade
import datetime

from trade import processors
from dateutil import relativedelta


def main():
    parser = argparse.ArgumentParser(description='Alpharius stock trading.')

    parser.add_argument('-m', '--mode', help='Running mode. Can be backtest or trade.',
                        required=True, choices=['backtest', 'trade'])
    parser.add_argument('--start_date', default=None,
                        help='Start date of the backtesting. Only used in backtest mode.')
    parser.add_argument('--end_date', default=None,
                        help='End date of the backtesting. Only used in backtest mode.')
    args = parser.parse_args()

    processor_factories = [
        processors.OvernightProcessorFactory(),
        processors.ZScoreProcessorFactory(),
        processors.O2lProcessorFactory(),
        processors.O2hProcessorFactory(),
        processors.BearMomentumProcessorFactory(),
    ]
    today = datetime.datetime.today()
    if args.mode == 'backtest':
        default_start_date = (
            today - relativedelta.relativedelta(years=1)).strftime('%F')
        default_end_date = (today + datetime.timedelta(days=1)).strftime('%F')
        start_date = args.start_date or default_start_date
        end_date = args.end_date or default_end_date
        backtesting = trade.Backtesting(start_date=start_date, end_date=end_date,
                                        processor_factories=processor_factories)
        backtesting.run()
    else:
        trading = trade.Trading(processor_factories=processor_factories)
        trading.run()


if __name__ == '__main__':
    main()
