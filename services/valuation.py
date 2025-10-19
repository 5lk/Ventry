import random

SCALE_GBP = 100

def fetch_company_valuation_gbp(company_name: str) -> float:
    base = 1_000_000.00
    jitter = random.uniform(-25_000.00, 25_000.00)
    return max(100_000.00, base + jitter)

def compute_token_price_scaled_gbp(company_name: str, supply: int, equity_pct: float, valuation_override_gbp: float | None = None) -> int:
    valuation_gbp = valuation_override_gbp if valuation_override_gbp is not None else fetch_company_valuation_gbp(company_name)
    per_token_gbp = (valuation_gbp * equity_pct) / max(1, supply)
    return int(round(per_token_gbp * SCALE_GBP))
