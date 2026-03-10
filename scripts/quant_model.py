#!/usr/bin/env python3
"""
Signal model and consensus gating for Polymarket fast markets.

Inputs:
- ohlcv: list[timestep][open, high, low, close, volume]
- order_book: list[timestep][feature...]
- price: list[timestep][price] or list[price]

Output:
- model prediction score
- heuristic decision
- consensus decision requiring both signals to agree
"""

from __future__ import annotations

from typing import Any, Sequence

import torch
from torch import Tensor, nn


def _ensure_sequence_tensor(
    data: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    feature_dim: int | None = None,
) -> Tensor:
    if isinstance(data, Tensor):
        tensor = data.detach().clone().float()
    else:
        tensor = torch.tensor(data, dtype=torch.float32)

    if tensor.ndim == 1:
        tensor = tensor.unsqueeze(-1)

    if tensor.ndim == 2:
        tensor = tensor.unsqueeze(0)
    elif tensor.ndim != 3:
        raise ValueError(
            "Expected input with shape [seq, feature] or [batch, seq, feature]"
        )

    if feature_dim is not None and tensor.shape[-1] != feature_dim:
        raise ValueError(
            f"Expected feature dimension {feature_dim}, got {tensor.shape[-1]}"
        )

    if tensor.shape[1] == 0:
        raise ValueError("Sequence length must be greater than 0")

    return tensor


def order_book_vector_from_summary(summary: dict[str, Any] | None) -> list[float]:
    """Convert the current order book summary into a fixed-size feature vector."""
    if not summary:
        return [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    bid_depth = float(summary.get("bid_depth_usd", 0.0) or 0.0)
    ask_depth = float(summary.get("ask_depth_usd", 0.0) or 0.0)
    depth_total = bid_depth + ask_depth
    imbalance = ((bid_depth - ask_depth) / depth_total) if depth_total > 0 else 0.0
    return [
        float(summary.get("best_bid", 0.0) or 0.0),
        float(summary.get("best_ask", 0.0) or 0.0),
        float(summary.get("spread_pct", 0.0) or 0.0),
        bid_depth,
        ask_depth,
        imbalance,
    ]


class QuantModel(nn.Module):
    """
    Deterministic PyTorch scoring model over market microstructure features.

    The model consumes time sequences, extracts a compact feature set with torch ops,
    and produces a directional score:
    - positive: bullish / YES bias
    - negative: bearish / NO bias
    """

    def __init__(self, order_book_dim: int = 6) -> None:
        super().__init__()
        self.order_book_dim = order_book_dim

    def forward(
        self,
        ohlcv: Sequence[Sequence[float]] | Tensor,
        order_book: Sequence[Sequence[float]] | Tensor,
        price: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    ) -> Tensor:
        ohlcv_tensor = _ensure_sequence_tensor(ohlcv, feature_dim=5)
        order_book_tensor = _ensure_sequence_tensor(
            order_book, feature_dim=self.order_book_dim
        )
        price_tensor = _ensure_sequence_tensor(price, feature_dim=1)

        batch_sizes = {
            ohlcv_tensor.shape[0],
            order_book_tensor.shape[0],
            price_tensor.shape[0],
        }
        if len(batch_sizes) != 1:
            raise ValueError("All inputs must share the same batch size")

        eps = 1e-6

        open_ = ohlcv_tensor[:, :, 0]
        high = ohlcv_tensor[:, :, 1]
        low = ohlcv_tensor[:, :, 2]
        close = ohlcv_tensor[:, :, 3]
        volume = ohlcv_tensor[:, :, 4]

        first_open = open_[:, 0].clamp_min(eps)
        last_close = close[:, -1]
        ohlcv_momentum = (last_close - first_open) / first_open
        candle_body_trend = ((close - open_) / open_.clamp_min(eps)).mean(dim=1)
        realized_vol = ((high - low) / open_.clamp_min(eps)).mean(dim=1)
        volume_ratio = volume[:, -1] / volume.mean(dim=1).clamp_min(eps)

        best_bid = order_book_tensor[:, -1, 0]
        best_ask = order_book_tensor[:, -1, 1]
        spread_pct = order_book_tensor[:, -1, 2]
        bid_depth = order_book_tensor[:, -1, 3]
        ask_depth = order_book_tensor[:, -1, 4]
        imbalance = order_book_tensor[:, -1, 5]
        depth_total = (bid_depth + ask_depth).clamp_min(eps)
        depth_confidence = depth_total / (depth_total + 1000.0)
        book_mid = (best_bid + best_ask) * 0.5

        poly_price = price_tensor[:, :, 0]
        last_price = poly_price[:, -1]
        mean_price = poly_price.mean(dim=1)
        price_trend = last_price - poly_price[:, 0]
        mispricing = 0.5 - last_price
        book_dislocation = book_mid - last_price

        score = (
            10.0 * ohlcv_momentum
            + 4.0 * candle_body_trend
            + 3.0 * mispricing.sign() * torch.relu(mispricing.abs() - 0.01)
            + 1.5 * imbalance * depth_confidence
            + 0.5 * book_dislocation
            + 0.2 * price_trend
            + 0.15 * (volume_ratio - 1.0)
            - 2.5 * spread_pct
            - 1.0 * realized_vol
        )
        return score

    @torch.inference_mode()
    def predict_value(
        self,
        ohlcv: Sequence[Sequence[float]] | Tensor,
        order_book: Sequence[Sequence[float]] | Tensor,
        price: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    ) -> float:
        prediction = self.forward(ohlcv=ohlcv, order_book=order_book, price=price)
        if prediction.ndim == 0:
            return float(prediction.item())
        if prediction.shape[0] != 1:
            raise ValueError("predict_value expects a single sample input")
        return float(prediction[0].item())


def build_quant_model(order_book_dim: int = 6) -> QuantModel:
    return QuantModel(order_book_dim=order_book_dim)


def compute_heuristic_signal(
    *,
    asset: str,
    momentum: dict[str, Any],
    market_yes_price: float,
    entry_threshold: float,
    min_momentum_pct: float,
    volume_confidence: bool,
    fee_rate: float,
    has_yes_token: bool,
    has_no_token: bool,
) -> dict[str, Any]:
    raw_direction = (momentum.get("direction") or "").lower()
    momentum_pct_signed = float(momentum.get("momentum_pct", 0.0) or 0.0)
    momentum_pct = abs(momentum_pct_signed)
    volume_ratio = float(momentum.get("volume_ratio", 1.0) or 1.0)

    result: dict[str, Any] = {
        "approved": False,
        "reason": None,
        "signal_direction": raw_direction,
        "proposed_side": None,
        "divergence": None,
        "trade_rationale": None,
        "min_divergence": None,
        "fee_adjusted_breakeven": None,
        "volume_note": "",
    }

    if raw_direction not in {"up", "down"}:
        result["reason"] = "invalid-direction"
        return result

    if momentum_pct < min_momentum_pct:
        result["reason"] = "momentum-too-weak"
        return result

    if raw_direction == "up":
        side = "yes"
        has_token = has_yes_token
        divergence = 0.50 + entry_threshold - market_yes_price
        trade_rationale = (
            f"{asset} up {momentum_pct_signed:+.3f}% but YES only ${market_yes_price:.3f}"
        )
    else:
        side = "no"
        has_token = has_no_token
        divergence = market_yes_price - (0.50 - entry_threshold)
        trade_rationale = (
            f"{asset} down {momentum_pct_signed:+.3f}% but YES still ${market_yes_price:.3f}"
        )

    result["proposed_side"] = side
    result["divergence"] = round(divergence, 6)
    result["trade_rationale"] = trade_rationale

    if not has_token:
        result["reason"] = f"missing-token-id-{side}"
        return result

    if volume_confidence and volume_ratio < 0.5:
        result["reason"] = "low-volume"
        return result

    if volume_confidence and volume_ratio > 2.0:
        result["volume_note"] = f" high-volume:{volume_ratio:.1f}x"

    if divergence <= 0:
        result["reason"] = "priced-in"
        return result

    if fee_rate > 0:
        buy_price = market_yes_price if side == "yes" else (1 - market_yes_price)
        win_profit = (1 - buy_price) * (1 - fee_rate)
        breakeven = buy_price / (win_profit + buy_price)
        fee_penalty = breakeven - 0.50
        min_divergence = fee_penalty + 0.02
        result["fee_adjusted_breakeven"] = round(breakeven, 6)
        result["min_divergence"] = round(min_divergence, 6)
        if divergence < min_divergence:
            result["reason"] = "fees-eat-edge"
            return result

    result["approved"] = True
    result["reason"] = "consensus-pending"
    return result


def model_signal_from_prediction(
    prediction: float,
    *,
    entry_threshold: float,
) -> dict[str, Any]:
    threshold = max(0.01, entry_threshold * 0.5)
    if prediction > threshold:
        return {
            "prediction": round(prediction, 6),
            "threshold": round(threshold, 6),
            "signal_direction": "up",
            "proposed_side": "yes",
            "approved": True,
        }
    if prediction < -threshold:
        return {
            "prediction": round(prediction, 6),
            "threshold": round(threshold, 6),
            "signal_direction": "down",
            "proposed_side": "no",
            "approved": True,
        }
    return {
        "prediction": round(prediction, 6),
        "threshold": round(threshold, 6),
        "signal_direction": "neutral",
        "proposed_side": None,
        "approved": False,
    }


def evaluate_consensus_signal(
    *,
    model: QuantModel,
    asset: str,
    ohlcv: Sequence[Sequence[float]] | Tensor,
    order_book: Sequence[Sequence[float]] | Tensor,
    price: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    momentum: dict[str, Any],
    market_yes_price: float,
    entry_threshold: float,
    min_momentum_pct: float,
    volume_confidence: bool,
    fee_rate: float,
    has_yes_token: bool,
    has_no_token: bool,
) -> dict[str, Any]:
    heuristic = compute_heuristic_signal(
        asset=asset,
        momentum=momentum,
        market_yes_price=market_yes_price,
        entry_threshold=entry_threshold,
        min_momentum_pct=min_momentum_pct,
        volume_confidence=volume_confidence,
        fee_rate=fee_rate,
        has_yes_token=has_yes_token,
        has_no_token=has_no_token,
    )

    prediction = model.predict_value(ohlcv=ohlcv, order_book=order_book, price=price)
    model_signal = model_signal_from_prediction(
        prediction,
        entry_threshold=entry_threshold,
    )

    agreed = (
        heuristic.get("approved")
        and model_signal.get("approved")
        and heuristic.get("signal_direction") == model_signal.get("signal_direction")
        and heuristic.get("proposed_side") == model_signal.get("proposed_side")
    )

    reason = heuristic.get("reason")
    if heuristic.get("approved") and not model_signal.get("approved"):
        reason = "model-neutral"
    elif heuristic.get("approved") and model_signal.get("approved") and not agreed:
        reason = "model-disagreement"
    elif agreed:
        reason = "consensus-ok"

    return {
        "approved": agreed,
        "reason": reason,
        "heuristic": heuristic,
        "model": model_signal,
    }


__all__ = [
    "QuantModel",
    "build_quant_model",
    "compute_heuristic_signal",
    "evaluate_consensus_signal",
    "model_signal_from_prediction",
    "order_book_vector_from_summary",
]
