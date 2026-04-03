import MetaTrader5 as mt5
import pandas as pd
import backtrader as bt
from datetime import datetime, timedelta, time
import MetaTrader5 as mt5
import pandas as pd


def get_mt5_data_by_date(symbol, timeframe, start_date_str, end_date_str):
    if not mt5.initialize():
        print("MT5 初始化失败")
        return None
        
    # 解析日期字符串 (例如 '20260101' -> datetime对象)
    # 因为是闭区间，结束日期需要加1天，确保包含最后一天的数据
    start_dt = datetime.strptime(start_date_str, '%Y%m%d')
    end_dt = datetime.strptime(end_date_str, '%Y%m%d') + timedelta(days=1)
    
    rates = mt5.copy_rates_range(symbol, timeframe, start_dt, end_dt)
    mt5.shutdown()
    
    if rates is None or len(rates) == 0:
        print(f"未能获取 {start_date_str} 到 {end_date_str} 的 {symbol} 数据")
        return None
        
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]
    df.rename(columns={'tick_volume': 'volume'}, inplace=True)
    df.set_index('time', inplace=True)
    return df

class BaseEvalStrategy(bt.Strategy):
    params = dict(
        daily_start_time='0000', # 默认 00:00 
        daily_end_time='2359',   # 默认 23:59
    )

    def __init__(self):
        # 解析交易时间窗口
        self.trade_start = datetime.strptime(self.p.daily_start_time, '%H%M').time()
        self.trade_end = datetime.strptime(self.p.daily_end_time, '%H%M').time()
        
        # 用于记录交易流水和每日数据的列表
        self.trade_records = []
        self.daily_records = {} # key: date, value: dict of stats

    def is_in_trading_window(self):
        """判断当前K线时间是否在允许交易的时间段内"""
        current_time = self.data.datetime.time(0)
        return self.trade_start <= current_time <= self.trade_end

    def notify_trade(self, trade):
        """Backtrader 内置回调：当一笔交易（一买一卖为一笔完整交易）关闭时触发"""
        if trade.isclosed:
            # 记录这笔完整交易的细节
            record = {
                'open_datetime': bt.num2date(trade.dtopen),
                'close_datetime': bt.num2date(trade.dtclose),
                'symbol': trade.data._name,
                'direction': 'Long' if trade.history[0].event.size > 0 else 'Short',
                'size': abs(trade.size),
                'open_price': trade.price,
                'close_price': trade.history[-1].status.price,
                'pnl': trade.pnl,       # 净盈亏
                'pnl_comm': trade.pnlcomm # 扣除手续费后的净盈亏
            }
            self.trade_records.append(record)

    def next(self):
        # 记录每日最低净值 (用于后续生成每日报告)
        current_date = self.data.datetime.date(0)
        current_value = self.broker.getvalue()
        
        if current_date not in self.daily_records:
            self.daily_records[current_date] = {'min_value': current_value}
        else:
            if current_value < self.daily_records[current_date]['min_value']:
                self.daily_records[current_date]['min_value'] = current_value
                
        # --- 子类将在这里实现具体的 buy/sell 逻辑 ---
        # 子类中会通过 if self.is_in_trading_window(): 来限制开仓


def run_evaluation(start_date, end_date, daily_start, daily_end, symbols, strategy_class):
    # 最终输出的字典
    results_dict = {
        'trade_details': None,
        'daily_summary': None,
        'global_metrics': None,
        'plot_data': None
    }
    
    # 初始化 Cerebro
    cerebro = bt.Cerebro(tradehistory=True)
    
    # 1. 加载所有品种的数据
    for symbol in symbols:
        df = get_mt5_data_by_date(symbol, mt5.TIMEFRAME_M15, start_date, end_date)
        if df is not None:
            data = bt.feeds.PandasData(dataname=df, name=symbol)
            cerebro.adddata(data)
            
    if not cerebro.datas:
        print("没有成功加载任何数据，请检查日期和品种。")
        return results_dict

    # 2. 注入策略并配置时间参数
    cerebro.addstrategy(strategy_class, 
                        daily_start_time=daily_start, 
                        daily_end_time=daily_end)
    
    # 3. 初始资金与手续费 (外汇通常有固定点差/手续费，这里为了评测准确性必须设置)
    cerebro.broker.setcash(10000.0)
    cerebro.broker.setcommission(commission=0.0001) # 假设极小的手续费/点差模拟
    
    # 4. 添加官方分析器以计算专业指标
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, timeframe=bt.TimeFrame.Days, _name='sharpe')
    
    # 5. 运行回测
    print(f"开始评测区间: {start_date} -> {end_date} | 交易时段: {daily_start}-{daily_end}")
    strats = cerebro.run()
    strat = strats[0] # 获取跑完的策略实例
    
    # ==========================
    # 组装输出结果
    # ==========================
    
    # 1. 交易明细表 (Trade Details)
    df_trades = pd.DataFrame(strat.trade_records)
    results_dict['trade_details'] = df_trades

    # 2. 每日数据总结表 (Daily Summary)
    # 利用 Pandas 对 trade_details 进行按日重采样和统计
    if not df_trades.empty:
        df_trades['close_date'] = df_trades['close_datetime'].dt.date
        
        daily_group = df_trades.groupby('close_date').agg(
            total_trades=('pnl', 'count'),
            win_trades=('pnl', lambda x: (x > 0).sum()),
            loss_trades=('pnl', lambda x: (x <= 0).sum()),
            total_pnl=('pnl', 'sum'),
            max_single_win=('pnl', 'max'),
            max_single_loss=('pnl', 'min')
        )
        
        # 合并我们在策略中记录的每日最低净值
        min_values = pd.DataFrame.from_dict(strat.daily_records, orient='index')
        daily_summary = daily_group.join(min_values)
        results_dict['daily_summary'] = daily_summary

    # 3. 全局专业指标 (Global Metrics)
    trade_analysis = strat.analyzers.trades.get_analysis()
    drawdown_analysis = strat.analyzers.drawdown.get_analysis()
    sharpe_analysis = strat.analyzers.sharpe.get_analysis()
    
    # 提取关键信息，加入防错处理（当交易次数过少时有些值可能不存在）
    total_net = trade_analysis.get('pnl', {}).get('net', {}).get('total', 0)
    win_rate = (trade_analysis.get('won', {}).get('total', 0) / trade_analysis.get('total', {}).get('closed', 1)) * 100
    
    results_dict['global_metrics'] = {
        'Initial Capital': 10000.0,
        'Final Capital': cerebro.broker.getvalue(),
        'Total Net PnL': total_net,
        'Max Drawdown (%)': drawdown_analysis.get('max', {}).get('drawdown', 0),
        'Win Rate (%)': win_rate,
        'Sharpe Ratio': sharpe_analysis.get('sharperatio', None),
        'Total Trades': trade_analysis.get('total', {}).get('closed', 0)
    }

    # 4. 绘图句柄 (Plot Data)
    # 我们直接把跑完的 cerebro 对象存下来。
    # 以后你想画图，直接调用 results['plot_data'].plot(style='candlestick') 即可！
    results_dict['plot_data'] = cerebro
    
    print("评测完成！")
    return results_dict


import pandas as pd
import mplfinance as mpf
from datetime import datetime, date

# 假设你之前的 my_utils.get_mt5_data_by_date 函数已定义

def plot_single_day_data(symbol, timeframe, plot_date_str):
    """
    单独获取并绘制特定一天的 K 线图
    :param symbol: 品种代码
    :param timeframe: 时间周期 (MT5 格式)
    :param plot_date_str: 想要绘制的日期，格式 YYYYMMDD
    """
    # 1. 单独拉取那一天的数据
    # 使用 my_utils.py 中你已定义好的逻辑 (内部已处理 dates_range 和 MT5 shutdown)
    df_day = get_mt5_data_by_date(symbol, timeframe, plot_date_str, plot_date_str)
    
    # 2. 检查数据
    if df_day is None or df_day.empty:
        print(f"未能获取 {plot_date_str} 的数据，或该日无交易数据。")
        return
        
    # my_utils 获取的数据已将 'time' 设为 Index，且列名已适配 Backtrader (volume -> volume)
    # mplfinance 默认需要 DatetimeIndex，且列名大写不限，但 Backtrader 更习惯小写，mplfinance 也能自动识别小写。
    # 这里我们确保 DataFrame 的 Index 是 DatetimeIndex
    df_day.index = pd.to_datetime(df_day.index)
    
    print(f"成功获取 {symbol} {plot_date_str} 数据，共 {len(df_day)} 条。正在绘图...")
    
    # 3. 绘制 K 线图
    # 配置 mplfinance 参数：
    # type='candle'：绘制 K 线
    # volume=True：在下方绘制成交量
    # style='classic'：经典配色风格
    # title: 图表标题
    # ylabel: Y轴标签 (价格)
    # ylabel_lower: 下方Y轴标签 (成交量)
    # figscale: 放大图表整体尺寸
    mpf.plot(df_day, type='candle', volume=True, style='classic', 
             title=f'{symbol} 15m Candlestick - {plot_date_str}',
             ylabel='Price', ylabel_lower='Volume',
             figscale=1.5)


import pandas as pd
import numpy as np
import mplfinance as mpf

def plot_single_day_with_trades(symbol, timeframe, plot_date_str, trade_details):
    """
    绘制包含买卖信号的单日 K 线图
    """
    # 1. 拉取单日 K 线数据
    df_day = get_mt5_data_by_date(symbol, timeframe, plot_date_str, plot_date_str)
    if df_day is None or df_day.empty:
        print(f"未能获取 {plot_date_str} 的数据。")
        return
    df_day.index = pd.to_datetime(df_day.index)
    
    # 2. 准备“图层”容器：创建与 K 线长度一样的空序列，全部填充为 NaN
    buy_markers = pd.Series(np.nan, index=df_day.index)
    sell_markers = pd.Series(np.nan, index=df_day.index)
    
    # 3. 解析目标日期，准备从交易明细中捞数据
    target_date = pd.to_datetime(plot_date_str).date()
    
    if trade_details is not None and not trade_details.empty:
        # 遍历所有交易记录
        for _, trade in trade_details.iterrows():
            
            # --- 寻找当天的【开仓】点 ---
            if trade['open_datetime'].date() == target_date:
                idx = trade['open_datetime']
                if idx in df_day.index:
                    # 为了不让箭头挡住 K 线实体，我们在真实价格上下稍微偏移一点点 (比如黄金偏移 0.5 美金)
                    offset = 0.5 
                    if trade['direction'] == 'Long':
                        buy_markers[idx] = trade['open_price'] - offset # 多头开仓：在下方画向上箭头
                    else:
                        sell_markers[idx] = trade['open_price'] + offset # 空头开仓：在上方画向下箭头
            
            # --- 寻找当天的【平仓】点 ---
            if trade['close_datetime'].date() == target_date:
                idx = trade['close_datetime']
                if idx in df_day.index:
                    offset = 0.5
                    if trade['direction'] == 'Long':
                        sell_markers[idx] = trade['close_price'] + offset # 多头平仓（卖出）：在上方画向下箭头
                    else:
                        buy_markers[idx] = trade['close_price'] - offset  # 空头平仓（买回）：在下方画向上箭头

    # 4. 打包我们的信号图层
    apds = []
    
    # 只有当那一天真的有买入动作时，才添加买入图层
    if not buy_markers.isna().all():
        # marker='^' 是向上的三角形，color='g' 是绿色，markersize 控制大小
        apds.append(mpf.make_addplot(buy_markers, type='scatter', markersize=30, marker='^', color='g'))
        
    # 只有当那一天真的有卖出动作时，才添加卖出图层
    if not sell_markers.isna().all():
        # marker='v' 是向下的三角形，color='r' 是红色
        apds.append(mpf.make_addplot(sell_markers, type='scatter', markersize=30, marker='v', color='r'))

    print(f"准备绘制 {plot_date_str} 的图表，找到 {len(apds)} 个交易动作信号...")

    # 手动计算均线
    df_day['MA5'] = df_day['close'].rolling(5).mean()
    df_day['MA20'] = df_day['close'].rolling(20).mean()

    # 加入到 apds 中
    apds.append(mpf.make_addplot(df_day['MA5'], color='blue', width=1.0))
    apds.append(mpf.make_addplot(df_day['MA20'], color='orange', width=1.5))
    
    # 5. 最终渲染：把 K 线和附加信号图层 (addplot) 拼在一起
    mpf.plot(df_day, type='candle', volume=True, style='classic', 
            #  mav=(5, 20),
             addplot=apds,
             title=f'{symbol} Trades Execution - {plot_date_str}',
             ylabel='Price', ylabel_lower='Volume', figscale=1.5)
    



