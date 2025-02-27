import logging
from decimal import Decimal
from typing import Dict, List

import pandas_ta as ta  # noqa: F401

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import BuyOrderCompletedEvent, OrderFilledEvent, SellOrderCompletedEvent
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class PMMhShiftedMidPriceDynamicSpread(ScriptStrategyBase):
    """
    Design Template: https://hummingbot-foundation.notion.site/Simple-PMM-with-shifted-mid-price-and-dynamic-spreads-63cc765486dd42228d3da0b32537fc92
    Video: -
    Description:
    The bot will place two orders around the `reference_price` (mid price or last traded price +- %based on `RSI` value )
    in a `trading_pair` on `exchange`, with a distance defined by the `spread` multiplied by `spreads_factors`
    based on `NATR`. Every `order_refresh_time` seconds, the bot will cancel and replace the orders.
    """
    # Define the variables that we are going to use for the spreads
    # spread = (NATR * natr_scalar) / 2
    natr_scalar = 5
    spread = 1

    # Define the price source and the multiplier that shifts the price
    # price_multiplier = (RSI - 50) / 50 * spread_base
    # reference_price = orig_price * (1 + price_multiplier)
    price_source = PriceType.BestBid
    orig_price = 1
    reference_price = 1
    price_multiplier = 1
    inventory_multipler = 1

    # Trading conf
    order_refresh_time = 55
    order_amount = 2
    trading_pair = "SEI-USDT"
    exchange = "binance"
    candle_exchange = "binance"
    candles_length = 8

    # Inventory variables
    target_ratio = 0.5
    inventory_delta = 0
    inventory_multiplier = 0

    # Creating instance of the candles
    candles = CandlesFactory.get_candle(connector=candle_exchange,
                                        trading_pair=trading_pair,
                                        interval="1m")

    # Variables to store the volume and quantity of orders
    total_sell_orders = 0
    total_buy_orders = 0
    total_sell_volume = 0
    total_buy_volume = 0
    create_timestamp = 0

    markets = {exchange: {trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        # Is necessary to start the Candles Feed.
        super().__init__(connectors)
        self.candles.start()

    def on_stop(self):
        """
        Without this functionality, the network iterator will continue running forever after stopping the strategy
        That's why is necessary to introduce this new feature to make a custom stop with the strategy.
        """
        # we are going to close all the open positions when the bot stops
        self.candles.stop()

    def on_tick(self):
        if self.create_timestamp <= self.current_timestamp and self.candles.is_ready:
            self.cancel_all_orders()
            self.update_multipliers()
            proposal: List[OrderCandidate] = self.create_proposal()
            proposal_adjusted: List[OrderCandidate] = self.adjust_proposal_to_budget(proposal)
            self.place_orders(proposal_adjusted)
            self.create_timestamp = self.order_refresh_time + self.current_timestamp

    def get_candles_with_features(self):
        candles_df = self.candles.candles_df
        candles_df.ta.rsi(length=self.candles_length, append=True)
        candles_df.ta.natr(length=self.candles_length, scalar=self.natr_scalar, append=True)
        return candles_df

    def update_multipliers(self):
        candles_df = self.get_candles_with_features()
        self.spread = candles_df[f"NATR_{self.candles_length}"].iloc[-1] / 2

        # Trend Price Shift
        rsi = candles_df[f"RSI_{self.candles_length}"].iloc[-1]
        if rsi > 70:
            self.price_multiplier = self.spread
        elif rsi < 30:
            self.price_multiplier = -self.spread
        else:
            self.price_multiplier = (rsi - 50) / 50 * self.spread

        # Inventory Price Shift
        base_bal = self.connectors[self.exchange].get_balance("SEI")
        base_bal_in_quote = base_bal * self.orig_price
        quote_bal = self.connectors[self.exchange].get_balance("USDT")
        current_ratio = float(base_bal_in_quote / (base_bal_in_quote + quote_bal))
        self.inventory_delta = (self.target_ratio - current_ratio) / self.target_ratio
        self.inventory_multiplier = self.inventory_delta * self.spread

    def create_proposal(self) -> List[OrderCandidate]:
        self.orig_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, self.price_source)
        best_bid = self.connectors[self.exchange].get_price(self.trading_pair, False)
        best_ask = self.connectors[self.exchange].get_price(self.trading_pair, True)
        self.reference_price = self.orig_price * Decimal(str(1 + self.price_multiplier)) * Decimal(str(1 + self.inventory_multiplier))
        buy_price = min(self.reference_price * Decimal(1 - self.spread), best_bid)
        sell_price = max(self.reference_price * Decimal(1 + self.spread), best_ask)

        buy_order = OrderCandidate(trading_pair=self.trading_pair, is_maker=True, order_type=OrderType.LIMIT,
                                   order_side=TradeType.BUY, amount=Decimal(self.order_amount), price=buy_price)

        sell_order = OrderCandidate(trading_pair=self.trading_pair, is_maker=True, order_type=OrderType.LIMIT,
                                    order_side=TradeType.SELL, amount=Decimal(self.order_amount), price=sell_price)

        return [buy_order, sell_order]

    def adjust_proposal_to_budget(self, proposal: List[OrderCandidate]) -> List[OrderCandidate]:
        proposal_adjusted = self.connectors[self.exchange].budget_checker.adjust_candidates(proposal, all_or_none=True)
        return proposal_adjusted

    def place_orders(self, proposal: List[OrderCandidate]) -> None:
        for order in proposal:
            if order.amount != 0:
                self.place_order(connector_name=self.exchange, order=order)
            else:
                self.logger().info(f"Not enough funds to place the {order.order_type} order")

    def place_order(self, connector_name: str, order: OrderCandidate):
        if order.order_side == TradeType.SELL:
            self.sell(connector_name=connector_name, trading_pair=order.trading_pair, amount=order.amount,
                      order_type=order.order_type, price=order.price)
        elif order.order_side == TradeType.BUY:
            self.buy(connector_name=connector_name, trading_pair=order.trading_pair, amount=order.amount,
                     order_type=order.order_type, price=order.price)

    def cancel_all_orders(self):
        for order in self.get_active_orders(connector_name=self.exchange):
            self.cancel(self.exchange, order.trading_pair, order.client_order_id)

    def did_fill_order(self, event: OrderFilledEvent):
        msg = (
            f"{event.trade_type.name} {round(event.amount, 2)} {event.trading_pair} {self.exchange} at {round(event.price, 2)}")
        self.log_with_clock(logging.INFO, msg)
        self.total_buy_volume += event.amount if event.trade_type == TradeType.BUY else 0
        self.total_sell_volume += event.amount if event.trade_type == TradeType.SELL else 0

    def did_complete_buy_order(self, event: BuyOrderCompletedEvent):
        self.total_buy_orders += 1

    def did_complete_sell_order(self, event: SellOrderCompletedEvent):
        self.total_sell_orders += 1

    def format_status(self) -> str:
        """
        Returns status of the current strategy on user balances and current active orders. This function is called
        when status command is issued. Override this function to create custom status display output.
        """
        if not self.ready_to_trade:
            return "Market connectors are not ready."
        lines = []

        balance_df = self.get_balance_df()
        lines.extend(["", "  Balances:"] + ["    " + line for line in balance_df.to_string(index=False).split("\n")])

        try:
            df = self.active_orders_df()
            lines.extend(["", "  Orders:"] + ["    " + line for line in df.to_string(index=False).split("\n")])
        except ValueError:
            lines.extend(["", "  No active maker orders."])
        spread_price = Decimal(self.spread) * Decimal(self.reference_price)
        trend_price_shift = Decimal(self.price_multiplier) * Decimal(self.reference_price)
        inventory_price_shift = Decimal(self.inventory_multiplier) * Decimal(self.reference_price)

        lines.extend(["\n-----------------------------------------------------------------------------------------------------------\n"])
        lines.extend(["", f"  Total Buy Orders: {self.total_buy_orders:.2f} | Total Sell Orders: {self.total_sell_orders:.2f}"])
        lines.extend(["", f"  Total Buy Volume: {self.total_buy_volume:.2f} | Total Sell Volume: {self.total_sell_volume:.2f}"])
        lines.extend(["\n-----------------------------------------------------------------------------------------------------------\n"])
        lines.extend(["", f"  Spread bps: {self.spread * 10000:.1f} | Spread Price: {spread_price:.4f}"])
        lines.extend(["", f"  Trending Price: {self.reference_price:.4f} | Trend Price Shift: {self.price_multiplier:.4f}"])
        lines.extend(["", f"  Target Inventory Ratio: {self.target_ratio:.4f} | Inventory Delta: {self.inventory_delta:.4f}"])
        lines.extend(["", f"  Orig Price: {self.orig_price:.4f} | Trend Shift: {trend_price_shift:.4f} | Inventory Shift: {inventory_price_shift:.4f} | Reference Price: {self.reference_price:.4f}"])
        lines.extend(["\n-----------------------------------------------------------------------------------------------------------\n"])
        candles_df = self.get_candles_with_features()
        lines.extend([f"Candles: {self.candles.name} | Interval: {self.candles.interval}"])
        lines.extend(["    " + line for line in candles_df.tail(self.candles_length).to_string(index=False).split("\n")])
        lines.extend(["\n-----------------------------------------------------------------------------------------------------------\n"])
        return "\n".join(lines)
