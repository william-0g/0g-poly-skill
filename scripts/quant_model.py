#!/usr/bin/env python3
"""
Trainable sequence model and consensus gating for Polymarket fast markets.

Inputs:
- ohlcv: list[timestep][open, high, low, close, volume]
- order_book: list[timestep][feature...]
- price: list[timestep][price] or list[price]

Output:
- model prediction score centered around 0.0
- heuristic decision
- consensus decision requiring both signals to agree
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import torch
from torch import Tensor, nn

try:
    from scripts.polymarket_features import (
        FeatureWindow,
        OnlineFeatureBuffer,
        extract_feature_window_from_live_events,
        extract_feature_window_from_parquet,
    )
except ImportError:
    from polymarket_features import (  # type: ignore[no-redef]
        FeatureWindow,
        OnlineFeatureBuffer,
        extract_feature_window_from_live_events,
        extract_feature_window_from_parquet,
    )

FACTOR_DIM = 10
FEATURE_SCHEMA_VERSION = 2


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


def _shift_right(values: Tensor) -> Tensor:
    return torch.cat((values[:, :1], values[:, :-1]), dim=1)


def _causal_mean(values: Tensor, window: int) -> Tensor:
    cumsum = torch.cumsum(values, dim=1)
    numer = cumsum.clone()
    numer[:, window:] = cumsum[:, window:] - cumsum[:, :-window]
    counts = torch.arange(
        1,
        values.shape[1] + 1,
        device=values.device,
        dtype=values.dtype,
    ).clamp(max=window)
    return numer / counts.unsqueeze(0)


class QuantModel(nn.Module):
    """
    GRU-based directional model over time-aligned market features.

    The model accepts three synchronized sequences:
    - ohlcv: 5 features per step
    - order_book: 6 features per step by default
    - price: 1 feature per step

    `forward()` returns raw logits. `predict_value()` returns a centered score in
    [-0.5, 0.5] based on the sigmoid probability.
    """

    def __init__(
        self,
        order_book_dim: int = 6,
        hidden_dim: int = 64,
        num_layers: int = 2,
        dropout: float = 0.10,
    ) -> None:
        super().__init__()
        self.order_book_dim = order_book_dim
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.dropout = dropout
        self.factor_dim = FACTOR_DIM
        self.input_dim = 5 + order_book_dim + 1 + self.factor_dim
        self.feature_schema_version = FEATURE_SCHEMA_VERSION

        self.register_buffer("feature_mean", torch.zeros(self.input_dim))
        self.register_buffer("feature_std", torch.ones(self.input_dim))

        gru_dropout = dropout if num_layers > 1 else 0.0
        self.encoder = nn.GRU(
            input_size=self.input_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=gru_dropout,
        )
        self.head = nn.Sequential(
            nn.LayerNorm(hidden_dim),
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim // 2, 1),
        )

    def set_feature_stats(
        self,
        feature_mean: Sequence[float] | Tensor,
        feature_std: Sequence[float] | Tensor,
    ) -> None:
        mean_tensor = torch.as_tensor(feature_mean, dtype=torch.float32).reshape(-1)
        std_tensor = torch.as_tensor(feature_std, dtype=torch.float32).reshape(-1)
        if mean_tensor.numel() != self.input_dim:
            raise ValueError(
                f"Expected {self.input_dim} feature means, got {mean_tensor.numel()}"
            )
        if std_tensor.numel() != self.input_dim:
            raise ValueError(
                f"Expected {self.input_dim} feature stds, got {std_tensor.numel()}"
            )

        self.feature_mean.copy_(mean_tensor)
        self.feature_std.copy_(std_tensor.clamp_min(1e-6))

    def compose_features(
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

        seq_lengths = {
            ohlcv_tensor.shape[1],
            order_book_tensor.shape[1],
            price_tensor.shape[1],
        }
        if len(seq_lengths) != 1:
            raise ValueError("All inputs must share the same sequence length")

        eps = 1e-6

        open_ = ohlcv_tensor[:, :, 0]
        high = ohlcv_tensor[:, :, 1]
        low = ohlcv_tensor[:, :, 2]
        close = ohlcv_tensor[:, :, 3]
        volume = ohlcv_tensor[:, :, 4]

        best_bid = order_book_tensor[:, :, 0]
        best_ask = order_book_tensor[:, :, 1]
        spread_pct = order_book_tensor[:, :, 2]
        bid_depth = order_book_tensor[:, :, 3]
        ask_depth = order_book_tensor[:, :, 4]
        imbalance = order_book_tensor[:, :, 5]
        poly_price = price_tensor[:, :, 0]

        prev_close = _shift_right(close)
        total_depth = (bid_depth + ask_depth).clamp_min(eps)
        mid = ((best_bid + best_ask) * 0.5).clamp_min(eps)
        microprice = (
            best_ask * bid_depth + best_bid * ask_depth
        ) / total_depth
        first_poly_price = poly_price[:, :1].clamp_min(eps)
        one_step_return = (close - prev_close) / prev_close.clamp_min(eps)
        range_pct = (high - low) / close.clamp_min(eps)
        first_close = close[:, :1].clamp_min(eps)
        cumulative_return = (close - first_close) / first_close
        momentum_3 = _causal_mean(one_step_return, 3)
        momentum_5 = _causal_mean(one_step_return, 5)
        momentum_spread = momentum_3 - momentum_5
        trend_persistence_5 = _causal_mean(torch.sign(one_step_return), 5)
        body_to_range = (close - open_) / (high - low).clamp_min(eps)

        volume_mean_5 = _causal_mean(volume, 5).clamp_min(eps)
        volume_surprise_5 = volume / volume_mean_5 - 1.0
        range_expansion_5 = range_pct / _causal_mean(range_pct, 5).clamp_min(eps) - 1.0
        spread_compression_5 = spread_pct / _causal_mean(spread_pct, 5).clamp_min(eps) - 1.0
        depth_ratio_5 = total_depth / _causal_mean(total_depth, 5).clamp_min(eps)
        depth_pressure = imbalance * depth_ratio_5

        vwap_num = torch.cumsum(close * volume, dim=1)
        vwap_den = torch.cumsum(volume, dim=1)
        fallback_vwap = _causal_mean(close, 5)
        vwap = torch.where(vwap_den > eps, vwap_num / vwap_den.clamp_min(eps), fallback_vwap)
        vwap_gap = (close - vwap) / vwap.clamp_min(eps)
        price_vs_microprice = (poly_price - microprice) / mid

        transformed_ohlcv = torch.stack(
            (
                (open_ - prev_close) / prev_close.clamp_min(eps),
                (high - prev_close) / prev_close.clamp_min(eps),
                (low - prev_close) / prev_close.clamp_min(eps),
                one_step_return,
                torch.log1p(volume.clamp_min(0.0)),
            ),
            dim=-1,
        )
        transformed_order_book = torch.stack(
            (
                (best_bid - mid) / mid,
                (best_ask - mid) / mid,
                spread_pct,
                torch.log1p(bid_depth.clamp_min(0.0)),
                torch.log1p(ask_depth.clamp_min(0.0)),
                imbalance,
            ),
            dim=-1,
        )
        transformed_price = ((poly_price - first_poly_price) / first_poly_price).unsqueeze(-1)

        factors = torch.stack(
            (
                cumulative_return,
                momentum_3,
                momentum_spread,
                trend_persistence_5,
                body_to_range,
                volume_surprise_5,
                range_expansion_5,
                spread_compression_5,
                depth_pressure,
                vwap_gap + price_vs_microprice,
            ),
            dim=-1,
        )

        combined = torch.cat((transformed_ohlcv, transformed_order_book, transformed_price, factors), dim=-1)
        return combined

    def prepare_features(
        self,
        ohlcv: Sequence[Sequence[float]] | Tensor,
        order_book: Sequence[Sequence[float]] | Tensor,
        price: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    ) -> Tensor:
        combined = self.compose_features(ohlcv=ohlcv, order_book=order_book, price=price)
        mean = self.feature_mean.to(device=combined.device).view(1, 1, -1)
        std = self.feature_std.to(device=combined.device).view(1, 1, -1)
        return (combined - mean) / std

    def forward(
        self,
        ohlcv: Sequence[Sequence[float]] | Tensor,
        order_book: Sequence[Sequence[float]] | Tensor,
        price: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    ) -> Tensor:
        features = self.prepare_features(ohlcv=ohlcv, order_book=order_book, price=price)
        _, hidden = self.encoder(features)
        final_hidden = hidden[-1]
        logits = self.head(final_hidden).squeeze(-1)
        return logits

    @torch.inference_mode()
    def predict_logit(
        self,
        ohlcv: Sequence[Sequence[float]] | Tensor,
        order_book: Sequence[Sequence[float]] | Tensor,
        price: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    ) -> float:
        prediction = self.forward(ohlcv=ohlcv, order_book=order_book, price=price)
        if prediction.ndim == 0:
            return float(prediction.item())
        if prediction.shape[0] != 1:
            raise ValueError("predict_logit expects a single sample input")
        return float(prediction[0].item())

    @torch.inference_mode()
    def predict_probability(
        self,
        ohlcv: Sequence[Sequence[float]] | Tensor,
        order_book: Sequence[Sequence[float]] | Tensor,
        price: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    ) -> float:
        logit = self.predict_logit(ohlcv=ohlcv, order_book=order_book, price=price)
        return float(torch.sigmoid(torch.tensor(logit)).item())

    @torch.inference_mode()
    def predict_value(
        self,
        ohlcv: Sequence[Sequence[float]] | Tensor,
        order_book: Sequence[Sequence[float]] | Tensor,
        price: Sequence[Sequence[float]] | Sequence[float] | Tensor,
    ) -> float:
        probability = self.predict_probability(
            ohlcv=ohlcv,
            order_book=order_book,
            price=price,
        )
        return probability - 0.5

    @torch.inference_mode()
    def predict_from_feature_window(self, feature_window: FeatureWindow) -> dict[str, Any]:
        probability = self.predict_probability(
            ohlcv=feature_window.ohlcv,
            order_book=feature_window.order_book,
            price=feature_window.price,
        )
        score = probability - 0.5
        return {
            "slug": feature_window.slug,
            "market_start_ts": feature_window.market_start_ts,
            "market_end_ts": feature_window.market_end_ts,
            "observed_end_ts": feature_window.observed_end_ts,
            "probability_up": probability,
            "score": score,
            "signal_direction": "up" if score > 0 else "down" if score < 0 else "neutral",
        }

    @torch.inference_mode()
    def predict_from_parquet_window(
        self,
        parquet_path: str | Path,
        *,
        market_end_ts: int | None = None,
        as_of_ts: int | None = None,
    ) -> dict[str, Any]:
        feature_window = extract_feature_window_from_parquet(
            parquet_path,
            market_end_ts=market_end_ts,
            as_of_ts=as_of_ts,
        )
        if feature_window is None:
            raise ValueError(f"Could not extract feature window from {parquet_path}")
        return self.predict_from_feature_window(feature_window)

    @torch.inference_mode()
    def predict_from_live_events(
        self,
        events: Sequence[dict[str, Any]],
        *,
        market_end_ts: int,
        as_of_ts: int | None = None,
        slug: str | None = None,
    ) -> dict[str, Any]:
        feature_window = extract_feature_window_from_live_events(
            events,
            market_end_ts=market_end_ts,
            as_of_ts=as_of_ts,
            slug=slug,
        )
        if feature_window is None:
            raise ValueError("Could not extract feature window from live events")
        return self.predict_from_feature_window(feature_window)

    @torch.inference_mode()
    def predict_from_live_buffer(
        self,
        feature_buffer: OnlineFeatureBuffer,
        *,
        as_of_ts: int | None = None,
    ) -> dict[str, Any]:
        feature_window = feature_buffer.build_feature_window(as_of_ts=as_of_ts)
        if feature_window is None:
            raise ValueError("Could not extract feature window from live buffer")
        return self.predict_from_feature_window(feature_window)


def build_quant_model(
    order_book_dim: int = 6,
    hidden_dim: int = 64,
    num_layers: int = 2,
    dropout: float = 0.10,
) -> QuantModel:
    return QuantModel(
        order_book_dim=order_book_dim,
        hidden_dim=hidden_dim,
        num_layers=num_layers,
        dropout=dropout,
    )


def load_quant_model(
    checkpoint_path: str | Path,
    map_location: str | torch.device = "cpu",
) -> QuantModel:
    checkpoint = torch.load(checkpoint_path, map_location=map_location)
    if not isinstance(checkpoint, dict) or "model_state_dict" not in checkpoint:
        raise ValueError("Checkpoint must contain model_state_dict")
    checkpoint_schema_version = int(checkpoint.get("feature_schema_version", 1) or 1)
    if checkpoint_schema_version != FEATURE_SCHEMA_VERSION:
        raise ValueError(
            "Checkpoint feature schema mismatch: "
            f"expected v{FEATURE_SCHEMA_VERSION}, got v{checkpoint_schema_version}. "
            "Retrain the model with the current quant_model.py."
        )

    model_kwargs = checkpoint.get("model_kwargs", {})
    model = build_quant_model(**model_kwargs)

    feature_mean = checkpoint.get("feature_mean")
    feature_std = checkpoint.get("feature_std")
    if feature_mean is not None and feature_std is not None:
        model.set_feature_stats(feature_mean, feature_std)

    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    return model


def predict_from_parquet_window(
    model: QuantModel,
    parquet_path: str | Path,
    *,
    market_end_ts: int | None = None,
    as_of_ts: int | None = None,
) -> dict[str, Any]:
    return model.predict_from_parquet_window(
        parquet_path,
        market_end_ts=market_end_ts,
        as_of_ts=as_of_ts,
    )


def predict_from_live_events(
    model: QuantModel,
    events: Sequence[dict[str, Any]],
    *,
    market_end_ts: int,
    as_of_ts: int | None = None,
    slug: str | None = None,
) -> dict[str, Any]:
    return model.predict_from_live_events(
        events,
        market_end_ts=market_end_ts,
        as_of_ts=as_of_ts,
        slug=slug,
    )


def predict_from_live_buffer(
    model: QuantModel,
    feature_buffer: OnlineFeatureBuffer,
    *,
    as_of_ts: int | None = None,
) -> dict[str, Any]:
    return model.predict_from_live_buffer(
        feature_buffer,
        as_of_ts=as_of_ts,
    )


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
        if momentum.get("momentum_consistent") is False:
            result["reason"] = "momentum-disagreement"
            return result
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
    model: QuantModel | None,
    asset: str,
    live_buffer: OnlineFeatureBuffer | None,
    live_buffer_as_of_ts: int | None,
    model_prediction: dict[str, Any] | None,
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

    if model is None:
        model_signal = {
            "prediction": None,
            "threshold": round(max(0.01, entry_threshold * 0.5), 6),
            "signal_direction": "disabled",
            "proposed_side": None,
            "approved": True,
            "reason": "model-disabled",
            "probability_up": None,
        }
    elif model_prediction is not None:
        prediction = float(model_prediction["score"])
        model_signal = model_signal_from_prediction(
            prediction,
            entry_threshold=entry_threshold,
        )
        model_signal["probability_up"] = round(
            float(model_prediction["probability_up"]),
            6,
        )
    elif live_buffer is None:
        model_signal = {
            "prediction": None,
            "threshold": round(max(0.01, entry_threshold * 0.5), 6),
            "signal_direction": "neutral",
            "proposed_side": None,
            "approved": False,
            "reason": "missing-live-buffer",
            "probability_up": None,
        }
    else:
        try:
            prediction_payload = model.predict_from_live_buffer(
                live_buffer,
                as_of_ts=live_buffer_as_of_ts,
            )
            prediction = float(prediction_payload["score"])
            model_signal = model_signal_from_prediction(
                prediction,
                entry_threshold=entry_threshold,
            )
            model_signal["probability_up"] = round(
                float(prediction_payload["probability_up"]),
                6,
            )
        except Exception as exc:
            model_signal = {
                "prediction": None,
                "threshold": round(max(0.01, entry_threshold * 0.5), 6),
                "signal_direction": "neutral",
                "proposed_side": None,
                "approved": False,
                "reason": f"live-buffer-prediction-failed:{exc}",
                "probability_up": None,
            }

    agreed = (
        heuristic.get("approved")
        and model_signal.get("approved")
        and (
            model_signal.get("reason") == "model-disabled"
            or (
                heuristic.get("signal_direction") == model_signal.get("signal_direction")
                and heuristic.get("proposed_side") == model_signal.get("proposed_side")
            )
        )
    )

    reason = heuristic.get("reason")
    model_reason = model_signal.get("reason")
    if heuristic.get("approved") and model_reason == "model-disabled":
        reason = "consensus-ok"
    elif heuristic.get("approved") and model_reason == "missing-live-buffer":
        reason = "missing-live-buffer"
    elif heuristic.get("approved") and isinstance(model_reason, str) and model_reason.startswith("live-buffer-prediction-failed:"):
        reason = model_reason
    elif heuristic.get("approved") and not model_signal.get("approved"):
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
    "load_quant_model",
    "model_signal_from_prediction",
    "order_book_vector_from_summary",
    "predict_from_live_buffer",
    "predict_from_live_events",
    "predict_from_parquet_window",
]
