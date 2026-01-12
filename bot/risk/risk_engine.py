import logging
import math

logger = logging.getLogger(__name__)


class RiskEngine:
    """Risk sizing + portfolio guardrails.

    Key principles:
    - Risk per trade is defined as *equity loss at stop* (not margin allocation).
    - Enforces max open positions and daily drawdown kill-switch.
    - Optional confidence-based sizing (small-band scaling only).
    """

    def __init__(self, config: dict):
        self.config = config.get("risk", {})

        # Core limits
        self.target_risk_pct = float(self.config.get("target_risk_per_trade_percent", 0.005))
        self.max_positions = int(self.config.get("max_open_positions", 3))
        self.leverage = float(self.config.get("leverage", 1))
        self.max_drawdown_daily_pct = float(self.config.get("max_drawdown_daily_percent", 3.0)) / 100.0
        self.max_position_pct = float(self.config.get("max_position_percent", 0.03))

        # Confidence sizing (throttle, not accelerator)
        self.use_conf_sizing = bool(self.config.get("use_confidence_sizing", False))
        self.min_risk_pct = float(self.config.get("min_risk_percent", 0.0025))
        self.max_risk_pct = float(self.config.get("max_risk_percent", 0.0075))
        self.min_conf = float(self.config.get("min_confidence_threshold", 0.20))
        self.conf_curve = str(self.config.get("confidence_curve", "squared")).lower()

        # Stop sanity: reject trades with stops too large vs ATR (if ATR passed in)
        self.max_stop_atr_mult = float(self.config.get("max_stop_atr_mult", 1.8))

        # Sanity caps to prevent accidental misconfiguration
        if self.target_risk_pct > 0.02:
            logger.warning(
                f"target_risk_per_trade_percent={self.target_risk_pct:.3f} is very high; capping to 0.02 (2%)."
            )
            self.target_risk_pct = 0.02

        if self.leverage < 1:
            self.leverage = 1

        # Kill-switch state
        self._kill_switch_active = False
        self._kill_switch_reason = None

    @property
    def is_killed(self) -> bool:
        return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> str:
        return self._kill_switch_reason

    def activate_kill_switch(self, reason: str):
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        logger.critical(f"KILL-SWITCH ACTIVATED: {reason}")

    def reset_kill_switch(self):
        self._kill_switch_active = False
        self._kill_switch_reason = None
        logger.info("Kill-switch has been reset.")

    def check_daily_drawdown(self, start_equity: float, current_equity: float) -> bool:
        if start_equity <= 0:
            logger.warning("Start equity is 0 or negative, cannot calculate drawdown.")
            return True

        drawdown = max(0.0, (start_equity - current_equity) / start_equity)
        drawdown_pct = drawdown * 100.0

        if drawdown > 0:
            logger.info(f"Daily Drawdown: {drawdown_pct:.2f}% (Limit: {self.max_drawdown_daily_pct * 100:.1f}%)")

        if drawdown >= self.max_drawdown_daily_pct:
            self.activate_kill_switch(
                f"Daily drawdown limit breached: {drawdown_pct:.2f}% >= {self.max_drawdown_daily_pct * 100:.1f}%"
            )
            return False

        return True

    def _effective_risk_pct(self, confidence: float | None) -> float:
        """Compute effective risk% for this trade."""
        base = self.target_risk_pct

        if not self.use_conf_sizing:
            return base

        if confidence is None:
            return base

        conf = float(confidence)
        if conf < self.min_conf:
            return 0.0  # caller should treat as reject

        # Conservative scaling
        if self.conf_curve == "linear":
            scaled = self.min_risk_pct + (self.max_risk_pct - self.min_risk_pct) * conf
        else:  # squared default
            scaled = self.min_risk_pct + (self.max_risk_pct - self.min_risk_pct) * (conf ** 2)

        # Always cap to [min, max]
        return max(self.min_risk_pct, min(self.max_risk_pct, scaled))

    def check_new_trade_allowed(self, current_positions: list) -> bool:
        if len(current_positions) >= self.max_positions:
            logger.info("Max open positions reached. Trade rejected.")
            return False
        return True

    def calculate_position_size(
        self,
        equity: float,
        entry_price: float,
        risk_metrics: dict,
        available_margin: float | None = None,
    ) -> float:
        """Position size in base units using stop distance sizing."""
        stop_loss = risk_metrics.get("stop_loss")
        if stop_loss is None:
            logger.warning("No Stop Loss provided, cannot calculate size.")
            return 0.0

        try:
            entry_price = float(entry_price)
            stop_loss = float(stop_loss)
        except Exception:
            logger.exception("Invalid entry_price/stop_loss types.")
            return 0.0

        stop_dist = abs(entry_price - stop_loss)
        if stop_dist <= 0:
            logger.warning("Stop distance is zero/negative; cannot size.")
            return 0.0

        # Optional ATR sanity check
        atr = risk_metrics.get("atr")
        if atr is not None:
            try:
                atr = float(atr)
                if atr > 0 and stop_dist > self.max_stop_atr_mult * atr:
                    logger.info(
                        f"Reject trade: stop_dist={stop_dist:.6f} > {self.max_stop_atr_mult:.2f}*ATR({atr:.6f})"
                    )
                    return 0.0
            except Exception:
                pass

        confidence = risk_metrics.get("confidence")
        risk_pct = self._effective_risk_pct(confidence)
        if risk_pct <= 0:
            logger.info(f"Reject trade: confidence below threshold ({confidence}).")
            return 0.0

        risk_amount = equity * risk_pct
        position_size = risk_amount / stop_dist

        # Notional & margin usage
        notional = position_size * entry_price
        margin_used = notional / self.leverage

        # Cap 1: Max margin as % of equity
        max_margin = equity * self.max_position_pct
        if margin_used > max_margin:
            max_notional = max_margin * self.leverage
            logger.info(
                f"Margin ${margin_used:.2f} exceeds cap ${max_margin:.2f}. Capping notional to ${max_notional:.2f}"
            )
            position_size = max_notional / entry_price
            notional = position_size * entry_price
            margin_used = notional / self.leverage

        # Cap 2: Check available margin if provided (with 10% buffer for fees)
        if available_margin is not None:
            usable_margin = float(available_margin) * 0.90  # 10% buffer for fees/slippage
            if margin_used > usable_margin:
                if usable_margin <= 0:
                    logger.warning(f"No available margin (available=${available_margin:.2f}). Cannot open position.")
                    return 0.0
                logger.info(
                    f"Margin ${margin_used:.2f} exceeds usable ${usable_margin:.2f} (90% of ${available_margin:.2f}). Reducing."
                )
                max_notional = usable_margin * self.leverage
                position_size = max_notional / entry_price

        if position_size <= 0 or math.isnan(position_size) or math.isinf(position_size):
            return 0.0

        return float(position_size)
