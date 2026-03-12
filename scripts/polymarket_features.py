#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd


SEQUENCE_MINUTES = 15
DEFAULT_SEQUENCE_STEPS = 15
DEFAULT_STEP_SECONDS = 60
FEATURE_COLUMNS = [
    "timestamp",
    "best_bid",
    "best_ask",
    "pc_price",
    "pc_size",
    "trade_price",
    "trade_size",
    "bid_sizes",
    "ask_sizes",
]


@dataclass
class FeatureWindow:
    slug: str | None
    market_start_ts: int
    market_end_ts: int
    observed_end_ts: int
    ohlcv: np.ndarray
    order_book: np.ndarray
    price: np.ndarray


@dataclass
class MinuteFeatureState:
    bucket_ts: int
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float = 0.0
    best_bid: float | None = None
    best_ask: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None


class OnlineFeatureBuffer:
    def __init__(
        self,
        *,
        market_end_ts: int,
        sequence_minutes: int = SEQUENCE_MINUTES,
        sequence_steps: int = DEFAULT_SEQUENCE_STEPS,
        step_seconds: int = DEFAULT_STEP_SECONDS,
        slug: str | None = None,
        target_asset_id: str | None = None,
        strict_asset_id: bool = False,
    ) -> None:
        self.market_end_ts = market_end_ts
        self.sequence_minutes = sequence_minutes
        self.sequence_steps = sequence_steps
        self.step_seconds = step_seconds
        self.market_start_ts = market_end_ts - sequence_steps * step_seconds
        self.slug = slug
        self.target_asset_id = str(target_asset_id) if target_asset_id is not None else None
        self.strict_asset_id = bool(strict_asset_id)
        self._minutes: dict[int, MinuteFeatureState] = {}
        self._latest_best_bid: float | None = None
        self._latest_best_ask: float | None = None
        self._latest_bid_depth: float | None = None
        self._latest_ask_depth: float | None = None
        self._latest_price: float | None = None
        self._latest_event_ts: int | None = None

    def ingest_event(self, event: dict[str, Any]) -> bool:
        event_asset_id = extract_event_asset_id(event)
        if self.target_asset_id is not None:
            if event_asset_id is None and self.strict_asset_id:
                return False
            if event_asset_id is not None and event_asset_id != self.target_asset_id:
                return False

        timestamp = _normalize_event_timestamp(event.get("timestamp"))
        if timestamp is None:
            return False
        if timestamp < self.market_start_ts or timestamp >= self.market_end_ts:
            return False

        bucket_ts = timestamp - (timestamp % self.step_seconds)
        minute_state = self._minutes.get(bucket_ts)
        if minute_state is None:
            minute_state = MinuteFeatureState(bucket_ts=bucket_ts)
            self._minutes[bucket_ts] = minute_state

        best_bid = _coerce_float(event.get("best_bid"))
        best_ask = _coerce_float(event.get("best_ask"))
        bid_depth = sum_depth(event.get("bid_sizes"))
        ask_depth = sum_depth(event.get("ask_sizes"))
        pc_price = _coerce_float(event.get("pc_price"))
        trade_price = _coerce_float(event.get("trade_price"))
        pc_size = _coerce_float(event.get("pc_size")) or 0.0
        trade_size = _coerce_float(event.get("trade_size")) or 0.0
        volume_delta = pc_size + trade_size

        if best_bid is not None:
            self._latest_best_bid = best_bid
            minute_state.best_bid = best_bid
        elif self._latest_best_bid is not None and minute_state.best_bid is None:
            minute_state.best_bid = self._latest_best_bid

        if best_ask is not None:
            self._latest_best_ask = best_ask
            minute_state.best_ask = best_ask
        elif self._latest_best_ask is not None and minute_state.best_ask is None:
            minute_state.best_ask = self._latest_best_ask

        if not np.isnan(bid_depth):
            self._latest_bid_depth = bid_depth
            minute_state.bid_depth = bid_depth
        elif self._latest_bid_depth is not None and minute_state.bid_depth is None:
            minute_state.bid_depth = self._latest_bid_depth

        if not np.isnan(ask_depth):
            self._latest_ask_depth = ask_depth
            minute_state.ask_depth = ask_depth
        elif self._latest_ask_depth is not None and minute_state.ask_depth is None:
            minute_state.ask_depth = self._latest_ask_depth

        price = trade_price
        if price is None:
            price = pc_price
        if price is None and best_bid is not None and best_ask is not None:
            price = (best_bid + best_ask) * 0.5
        if price is None and self._latest_best_bid is not None and self._latest_best_ask is not None:
            price = (self._latest_best_bid + self._latest_best_ask) * 0.5

        if price is not None:
            self._latest_price = price
            if minute_state.open is None:
                minute_state.open = price
                minute_state.high = price
                minute_state.low = price
            else:
                minute_state.high = max(minute_state.high or price, price)
                minute_state.low = min(minute_state.low or price, price)
            minute_state.close = price
        elif self._latest_price is not None and minute_state.open is None:
            minute_state.open = self._latest_price
            minute_state.high = self._latest_price
            minute_state.low = self._latest_price
            minute_state.close = self._latest_price

        minute_state.volume += volume_delta
        self._latest_event_ts = timestamp
        return True

    def ingest_events(self, events: Sequence[dict[str, Any]]) -> int:
        accepted = 0
        for event in events:
            if self.ingest_event(event):
                accepted += 1
        return accepted

    def build_feature_window(self, *, as_of_ts: int | None = None) -> FeatureWindow | None:
        observed_end_ts = min(as_of_ts or self.market_end_ts, self.market_end_ts)
        if observed_end_ts <= self.market_start_ts:
            return None

        minute_timestamps = [
            self.market_start_ts + index * self.step_seconds
            for index in range(self.sequence_steps)
        ]
        observed_minute_cutoff = observed_end_ts - (observed_end_ts % self.step_seconds)

        ohlcv_rows: list[list[float]] = []
        order_book_rows: list[list[float]] = []
        price_rows: list[list[float]] = []

        last_open: float | None = None
        last_high: float | None = None
        last_low: float | None = None
        last_close: float | None = None
        last_best_bid: float | None = None
        last_best_ask: float | None = None
        last_bid_depth: float | None = None
        last_ask_depth: float | None = None

        seen_live_minute = False

        for minute_ts in minute_timestamps:
            if minute_ts > observed_minute_cutoff:
                break

            state = self._minutes.get(minute_ts)
            if state is not None:
                if state.open is not None:
                    last_open = state.open
                    last_high = state.high
                    last_low = state.low
                    last_close = state.close
                    seen_live_minute = True
                if state.best_bid is not None:
                    last_best_bid = state.best_bid
                if state.best_ask is not None:
                    last_best_ask = state.best_ask
                if state.bid_depth is not None:
                    last_bid_depth = state.bid_depth
                if state.ask_depth is not None:
                    last_ask_depth = state.ask_depth
                volume = state.volume
            else:
                volume = 0.0

            if last_close is None or last_best_bid is None or last_best_ask is None:
                continue

            if last_open is None:
                last_open = last_close
            if last_high is None:
                last_high = last_close
            if last_low is None:
                last_low = last_close
            if last_bid_depth is None:
                last_bid_depth = 0.0
            if last_ask_depth is None:
                last_ask_depth = 0.0

            mid = max((last_best_bid + last_best_ask) * 0.5, 1e-6)
            spread_pct = max((last_best_ask - last_best_bid) / mid, 0.0)
            depth_total = max(last_bid_depth + last_ask_depth, 1e-6)
            imbalance = (last_bid_depth - last_ask_depth) / depth_total

            ohlcv_rows.append([last_open, last_high, last_low, last_close, volume])
            order_book_rows.append(
                [last_best_bid, last_best_ask, spread_pct, last_bid_depth, last_ask_depth, imbalance]
            )
            price_rows.append([last_close])

        if not seen_live_minute or len(ohlcv_rows) == 0:
            return None

        while len(ohlcv_rows) < self.sequence_steps:
            ohlcv_rows.insert(0, ohlcv_rows[0])
            order_book_rows.insert(0, order_book_rows[0])
            price_rows.insert(0, price_rows[0])

        return FeatureWindow(
            slug=self.slug,
            market_start_ts=self.market_start_ts,
            market_end_ts=self.market_end_ts,
            observed_end_ts=observed_end_ts,
            ohlcv=np.asarray(ohlcv_rows[-self.sequence_steps :], dtype=np.float32),
            order_book=np.asarray(order_book_rows[-self.sequence_steps :], dtype=np.float32),
            price=np.asarray(price_rows[-self.sequence_steps :], dtype=np.float32),
        )


def infer_market_end_ts_from_slug(slug_or_path: str | Path) -> int:
    stem = Path(slug_or_path).stem
    return int(stem.split("-")[-1])


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(result):
        return None
    return result


def _normalize_event_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if np.isnan(numeric):
            return None
        if numeric > 1e12:
            numeric /= 1000.0
        return int(numeric)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            numeric = float(stripped)
            if numeric > 1e12:
                numeric /= 1000.0
            return int(numeric)
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return int(parsed.timestamp())


def extract_event_asset_id(event: dict[str, Any] | None) -> str | None:
    if not isinstance(event, dict):
        return None
    for key in ("asset_id", "assetId", "clob_token_id", "clobTokenId", "token_id", "tokenId"):
        value = event.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def sum_depth(values: object, levels: int = 5) -> float:
    if values is None or (isinstance(values, float) and np.isnan(values)):
        return np.nan
    arr = np.asarray(values, dtype=np.float32)
    if arr.size == 0:
        return np.nan
    return float(arr[:levels].sum())


def price_series_from_frame(frame: pd.DataFrame) -> pd.Series:
    mid = (frame["best_bid"] + frame["best_ask"]) * 0.5
    return frame["trade_price"].combine_first(frame["pc_price"]).combine_first(mid)


def _normalize_timestamp_series(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    return parsed.dt.tz_convert("UTC").dt.tz_localize(None)


def prepare_event_frame(frame: pd.DataFrame) -> pd.DataFrame:
    working = frame.copy()
    for column in FEATURE_COLUMNS:
        if column not in working.columns:
            working[column] = np.nan

    working["timestamp"] = _normalize_timestamp_series(working["timestamp"])
    working = working.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if working.empty:
        return working

    working["bid_depth"] = working["bid_sizes"].apply(sum_depth)
    working["ask_depth"] = working["ask_sizes"].apply(sum_depth)
    working["price"] = price_series_from_frame(working)
    working["volume"] = working["trade_size"].fillna(0.0) + working["pc_size"].fillna(0.0)
    return working


def extract_feature_window_from_frame(
    frame: pd.DataFrame,
    *,
    market_end_ts: int,
    as_of_ts: int | None = None,
    sequence_minutes: int = SEQUENCE_MINUTES,
    slug: str | None = None,
) -> FeatureWindow | None:
    prepared = prepare_event_frame(frame)
    if prepared.empty:
        return None

    market_start_ts = market_end_ts - sequence_minutes * 60
    observed_end_ts = min(as_of_ts or market_end_ts, market_end_ts)
    if observed_end_ts <= market_start_ts:
        return None

    market_start = pd.Timestamp(market_start_ts, unit="s")
    observed_end = pd.Timestamp(observed_end_ts, unit="s")
    minute_index = pd.date_range(start=market_start, periods=sequence_minutes, freq="1min")

    active = prepared[
        (prepared["timestamp"] >= market_start) & (prepared["timestamp"] < observed_end)
    ].copy()
    if active.empty:
        return None

    fill_cols = ["best_bid", "best_ask", "bid_depth", "ask_depth", "price"]
    active[fill_cols] = active[fill_cols].ffill().bfill()
    if active[fill_cols].isna().any().any():
        return None

    mid = ((active["best_bid"] + active["best_ask"]) * 0.5).clip(lower=1e-6)
    active["spread_pct"] = ((active["best_ask"] - active["best_bid"]) / mid).clip(lower=0.0)
    depth_total = (active["bid_depth"] + active["ask_depth"]).clip(lower=1e-6)
    active["imbalance"] = (active["bid_depth"] - active["ask_depth"]) / depth_total

    active = active.set_index("timestamp")
    ohlc = active["price"].resample("1min").ohlc().reindex(minute_index).ffill().bfill()
    volume = active["volume"].resample("1min").sum().reindex(minute_index).fillna(0.0)
    book = (
        active[["best_bid", "best_ask", "spread_pct", "bid_depth", "ask_depth", "imbalance"]]
        .resample("1min")
        .last()
        .reindex(minute_index)
        .ffill()
        .bfill()
    )
    if ohlc.isna().any().any() or book.isna().any().any():
        return None

    ohlcv = np.column_stack(
        [
            ohlc["open"].to_numpy(dtype=np.float32),
            ohlc["high"].to_numpy(dtype=np.float32),
            ohlc["low"].to_numpy(dtype=np.float32),
            ohlc["close"].to_numpy(dtype=np.float32),
            volume.to_numpy(dtype=np.float32),
        ]
    )
    order_book = book.to_numpy(dtype=np.float32)
    price = ohlc["close"].to_numpy(dtype=np.float32).reshape(-1, 1)

    return FeatureWindow(
        slug=slug,
        market_start_ts=market_start_ts,
        market_end_ts=market_end_ts,
        observed_end_ts=observed_end_ts,
        ohlcv=ohlcv,
        order_book=order_book,
        price=price,
    )


def extract_feature_window_from_parquet(
    parquet_path: str | Path,
    *,
    market_end_ts: int | None = None,
    as_of_ts: int | None = None,
    sequence_minutes: int = SEQUENCE_MINUTES,
) -> FeatureWindow | None:
    path = Path(parquet_path)
    frame = pd.read_parquet(path, columns=FEATURE_COLUMNS)
    resolved_end_ts = market_end_ts or infer_market_end_ts_from_slug(path)
    return extract_feature_window_from_frame(
        frame,
        market_end_ts=resolved_end_ts,
        as_of_ts=as_of_ts,
        sequence_minutes=sequence_minutes,
        slug=path.stem,
    )


def extract_feature_window_from_live_events(
    events: Sequence[dict[str, Any]],
    *,
    market_end_ts: int,
    as_of_ts: int | None = None,
    sequence_minutes: int = SEQUENCE_MINUTES,
    slug: str | None = None,
) -> FeatureWindow | None:
    frame = pd.DataFrame(list(events))
    if frame.empty:
        return None
    return extract_feature_window_from_frame(
        frame,
        market_end_ts=market_end_ts,
        as_of_ts=as_of_ts,
        sequence_minutes=sequence_minutes,
        slug=slug,
    )


__all__ = [
    "FEATURE_COLUMNS",
    "FeatureWindow",
    "MinuteFeatureState",
    "OnlineFeatureBuffer",
    "SEQUENCE_MINUTES",
    "extract_event_asset_id",
    "extract_feature_window_from_frame",
    "extract_feature_window_from_live_events",
    "extract_feature_window_from_parquet",
    "infer_market_end_ts_from_slug",
    "prepare_event_frame",
    "price_series_from_frame",
    "sum_depth",
]
