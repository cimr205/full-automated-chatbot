from __future__ import annotations

"""
Learning-layer interface (spec section 32). A deliberate placeholder for a
future model trained on the trade journal — it does nothing yet.

Hard rule from the spec: ML may later ESTIMATE probability_of_success or
expected_R, but the risk engine always has final say over whether a trade
executes. This module exists so that boundary is explicit in code: nothing
in scoring.py or risk_exposure.py calls MLEstimator today, and wiring a
real model in later means ADDING a call site as an extra input to the
score, not replacing any existing deterministic check.
"""
from typing import Optional


class MLEstimator:
    """No-op until a real model is trained and wired in."""

    def estimate_probability(self, features: dict) -> Optional[float]:
        return None

    def estimate_expected_r(self, features: dict) -> Optional[float]:
        return None
