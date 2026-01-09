# TODO: Future Improvements

## Confidence-Based Position Sizing

**Status:** Pending

### Current Approach (Static)
```
Risk Amount = Equity × target_risk_pct (e.g., 20%)
Position Size = Risk Amount / |Entry - SL|
```
Every trade uses the same 20% risk regardless of confidence.

### Proposed Approach (Dynamic)

**Formula:**
```
Adjusted Risk % = min_risk + (max_risk - min_risk) × confidence
```

**Example with defaults:**
- `min_risk_pct` = 0.5% (floor - never risk less)
- `max_risk_pct` = 20% (ceiling - your current config)
- `confidence` = 0.0 to 1.0

| Confidence | Calculation | Risk % |
|------------|-------------|--------|
| 0.0 | 0.5 + (20 - 0.5) × 0.0 | **0.5%** |
| 0.25 | 0.5 + (20 - 0.5) × 0.25 | **5.4%** |
| 0.50 | 0.5 + (20 - 0.5) × 0.50 | **10.25%** |
| 0.75 | 0.5 + (20 - 0.5) × 0.75 | **15.1%** |
| 1.0 | 0.5 + (20 - 0.5) × 1.0 | **20%** |

### Additional Safeguards

1. **Minimum confidence threshold** (e.g., 0.1)
   - Skip trades with confidence < 10%
   - Prevents trading on weak signals

2. **Optional: Confidence curve** (linear vs exponential)
   - Linear: `risk = min + (max - min) × confidence`
   - Exponential: `risk = min + (max - min) × confidence²` (more conservative)

### Config Options to Add
```yaml
risk:
  use_confidence_sizing: true      # Enable/disable feature
  min_risk_percent: 0.005          # 0.5% floor
  max_risk_percent: 0.20           # 20% ceiling (same as target)
  min_confidence_threshold: 0.10   # Skip if confidence < 10%
```

### Files to Modify
1. `bot/risk/risk_engine.py` - Add confidence parameter to `calculate_position_size()`
2. `bot/execution/executor.py` - Pass confidence from signal to risk engine
3. `bot/strategies/router.py` - Ensure confidence is included in signal
4. `config.yaml` - Add new config options

### Visual
```
Risk %
  20% |                    *------ max_risk (confidence=1.0)
      |                 *
      |              *
      |           *
      |        *
  0.5%|*-------------------------- min_risk (confidence=0.0)
      +---------------------------
        0    0.25   0.5   0.75   1.0
                Confidence
```
