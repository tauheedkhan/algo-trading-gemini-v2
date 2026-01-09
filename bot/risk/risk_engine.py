import logging
import math

logger = logging.getLogger(__name__)


class RiskEngine:
    def __init__(self, config: dict):
        self.config = config.get("risk", {})
        self.target_risk_pct = self.config.get("target_risk_per_trade_percent", 0.02)
        self.max_positions = self.config.get("max_open_positions", 3)
        self.leverage = self.config.get("leverage", 1)
        self.max_drawdown_daily_pct = self.config.get("max_drawdown_daily_percent", 5.0) / 100
        self.max_position_pct = self.config.get("max_position_percent", 0.25)  # Max 25% of equity per position

        # Kill-switch state
        self._kill_switch_active = False
        self._kill_switch_reason = None

    @property
    def is_killed(self) -> bool:
        """Returns True if kill-switch is active."""
        return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> str:
        """Returns the reason for kill-switch activation."""
        return self._kill_switch_reason

    def activate_kill_switch(self, reason: str):
        """Activates the kill-switch, halting all trading."""
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        logger.critical(f"KILL-SWITCH ACTIVATED: {reason}")

    def reset_kill_switch(self):
        """Resets kill-switch (manual intervention required)."""
        self._kill_switch_active = False
        self._kill_switch_reason = None
        logger.info("Kill-switch has been reset.")

    def check_daily_drawdown(self, start_equity: float, current_equity: float) -> bool:
        """
        Checks if daily drawdown limit is breached.
        Returns True if trading should continue, False if kill-switch should activate.
        """
        if start_equity <= 0:
            logger.warning("Start equity is 0 or negative, cannot calculate drawdown.")
            return True  # Cannot calculate, allow trading (edge case)

        drawdown = (start_equity - current_equity) / start_equity
        drawdown_pct = drawdown * 100

        if drawdown > 0:
            logger.info(f"Daily Drawdown: {drawdown_pct:.2f}% (Limit: {self.max_drawdown_daily_pct * 100:.1f}%)")

        if drawdown >= self.max_drawdown_daily_pct:
            self.activate_kill_switch(
                f"Daily drawdown limit breached: {drawdown_pct:.2f}% >= {self.max_drawdown_daily_pct * 100:.1f}%"
            )
            return False

        return True
        
    def calculate_position_size(self, equity: float, entry_price: float, risk_metrics: dict,
                                  available_margin: float = None) -> float:
        """
        Calculates position size (in base asset) based on Volatility Targeting.

        Formula:
        Risk Amount = Equity * Target Risk %
        Position Size (Units) = Risk Amount / (ATR * Multiplier) OR Risk Amount / Stop Dist

        Using Stop Distance from Signal is safer/more direct.
        Size = (Equity * Risk_%) / |Entry - SL|
        """
        stop_loss = risk_metrics.get("stop_loss")
        if not stop_loss:
            logger.warning("No Stop Loss provided, cannot calculate size.")
            return 0.0

        risk_per_share = abs(entry_price - stop_loss)
        if risk_per_share == 0:
            return 0.0

        risk_amount_usd = equity * self.target_risk_pct

        position_size = risk_amount_usd / risk_per_share
        notional = position_size * entry_price
        margin_used = notional / self.leverage  # Actual balance used (before leverage)

        # Cap 1: Max margin (actual balance) as % of equity
        # e.g., $500 equity, 25% limit, 10x leverage = max $125 margin = $1250 notional
        max_margin = equity * self.max_position_pct
        if margin_used > max_margin:
            max_notional = max_margin * self.leverage
            logger.info(f"Margin ${margin_used:.0f} exceeds {self.max_position_pct*100:.0f}% limit (${max_margin:.0f}). Capping notional to ${max_notional:.0f}")
            position_size = max_notional / entry_price
            notional = position_size * entry_price
            margin_used = notional / self.leverage

        # Cap 2: Check available margin (actual free balance on exchange)
        if available_margin is not None:
            # Leave 10% buffer for fees and slippage
            usable_margin = available_margin * 0.90
            if margin_used > usable_margin:
                if usable_margin <= 0:
                    logger.warning(f"No available margin (available=${available_margin:.0f}). Cannot open position.")
                    return 0.0
                max_notional = usable_margin * self.leverage
                logger.info(f"Margin ${margin_used:.0f} exceeds available ${usable_margin:.0f}. Capping notional to ${max_notional:.0f}")
                position_size = max_notional / entry_price
                notional = position_size * entry_price
                margin_used = notional / self.leverage

        logger.info(f"Position: notional=${notional:.0f}, margin=${margin_used:.0f}, leverage={self.leverage}x")

        return position_size

    def check_new_trade_allowed(self, current_positions: list) -> bool:
        if len(current_positions) >= self.max_positions:
            logger.info("Max open positions reached. Trade rejected.")
            return False
        return True

risk_engine = RiskEngine({}) # Will be re-inited with real config
