from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from dataclasses import replace
from threading import Event, Thread, current_thread
from time import sleep

import pandas as pd

from app.broker.authentication import AccessToken, require_credentials
from app.broker.kite_client import KiteClient
from app.broker.market_data import MarketData
from app.database.database import Database
from app.database.models import DecisionLogRecord, NotificationRecord, PreSpikeEventRecord, SignalEngineStatus, SignalRecord
from app.database.repository import Repository
from app.market.candles import validate_ohlcv
from app.monitoring.notifications import Notifier, notification_message
from app.strategy.base import NoSignal, Strategy
from app.strategy.crossover import CrossoverStrategy
from app.strategy.ema_9_200_progressive import Ema9200ProgressiveStrategy
from app.strategy.pre_spike_momentum import PreSpikeMomentumStrategy


logger = logging.getLogger(__name__)


def refresh_selected_watchlist_tokens(repository: Repository, user_id: str, broker_client) -> tuple[str, ...]:
    """Refresh persisted equity tokens and remove symbols no longer listed by Kite."""
    instruments = pd.DataFrame(broker_client.instruments())
    required = {"tradingsymbol", "instrument_token", "exchange"}
    missing = required - set(instruments.columns)
    if missing:
        raise ValueError(f"Kite instrument response missing columns: {sorted(missing)}")
    instruments["exchange"] = instruments["exchange"].astype(str).str.strip().str.upper()
    instruments["tradingsymbol"] = instruments["tradingsymbol"].astype(str).str.strip().str.upper()
    if "instrument_type" in instruments.columns:
        instruments = instruments.loc[instruments["instrument_type"].isin(["EQ", "Equity"])]
    instruments = instruments.loc[instruments["exchange"].isin(["NSE", "BSE"])]
    current_by_key = {
        f"{row.exchange}:{row.tradingsymbol}": int(row.instrument_token)
        for row in instruments.itertuples()
        if row.tradingsymbol and pd.notna(row.instrument_token)
    }
    stale_symbols: list[str] = []

    def refresh_symbols(symbols: dict[str, int]) -> dict[str, int]:
        refreshed: dict[str, int] = {}
        for stored_symbol in symbols:
            raw_symbol = str(stored_symbol).strip().upper()
            exchange, tradingsymbol = raw_symbol.split(":", 1) if ":" in raw_symbol else ("NSE", raw_symbol)
            key = f"{exchange}:{tradingsymbol}"
            token = current_by_key.get(key)
            if token is None:
                stale_symbols.append(key)
                continue
            refreshed[key] = token
        return refreshed

    for watchlist in repository.load_watchlists(user_id, selected_only=True):
        refreshed = refresh_symbols(watchlist.symbols)
        if refreshed != watchlist.symbols:
            repository.update_watchlist_symbols(user_id, watchlist.name, refreshed)
    dynamic_watchlist = repository.load_dynamic_watchlist(user_id)
    if dynamic_watchlist and dynamic_watchlist.selected:
        refreshed = refresh_symbols(dynamic_watchlist.symbols)
        if refreshed != dynamic_watchlist.symbols:
            repository.update_dynamic_watchlist_symbols(user_id, refreshed)
    return tuple(dict.fromkeys(stale_symbols))


class AlwaysOnSignalEngine:
    def __init__(self, settings, repository: Repository, candle_loader: Callable[[int, str, int], pd.DataFrame], notifier: Notifier | None = None, user_id: str | None = None, strategy: Strategy | None = None, progressive_strategy: Ema9200ProgressiveStrategy | None = None, legacy_signals_enabled: bool = True, enable_progressive_strategy: bool | None = None):
        self.settings = settings
        self.repository = repository
        self.candle_loader = candle_loader
        self.notifier = notifier or Notifier(
            bool(getattr(settings, "enable_telegram", False)),
            getattr(settings, "telegram_bot_token", "").get_secret_value() if hasattr(getattr(settings, "telegram_bot_token", ""), "get_secret_value") else str(getattr(settings, "telegram_bot_token", "")),
            str(getattr(settings, "telegram_chat_id", "")),
        )
        self.user_id = user_id
        self.strategy = strategy or (CrossoverStrategy() if legacy_signals_enabled else None)
        self.progressive_strategy = progressive_strategy
        progressive_enabled = getattr(settings, "enable_ema_progressive_strategy", True) if enable_progressive_strategy is None else enable_progressive_strategy
        if enable_progressive_strategy is False:
            self.progressive_strategy = None
        elif self.progressive_strategy is None and progressive_enabled:
            self.progressive_strategy = Ema9200ProgressiveStrategy(settings.signal_timeframe)
        self.last_run_at: datetime | None = None
        self.last_error = ""
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._last_candles: set[tuple[str, int, str]] = set()
        self.last_heartbeat_at: datetime | None = None
        self._last_persisted_heartbeat_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="always-on-signal-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> bool:
        self._stop_event.set()
        if self._thread is not None and self._thread is not current_thread():
            self._thread.join(timeout=timeout)
        stopped = not self.running
        if stopped:
            self._thread = None
        return stopped

    def run_once(self, now: datetime | None = None) -> int:
        timestamp = now or datetime.now()
        self._heartbeat(datetime.now())
        if not self._market_open(timestamp):
            self._save_status(timestamp, self.last_heartbeat_at)
            return 0
        generated = 0
        cycle_errors: list[str] = []
        try:
            user_ids = [self.user_id] if self.user_id else self.repository.load_selected_watchlist_users()
            for user_id in user_ids:
                passed_watchlist: dict[str, int] = {}
                for watchlist in self.repository.load_watchlists(user_id, selected_only=True):
                    passed_watchlist.update(watchlist.symbols)
                dynamic_watchlist = self.repository.load_dynamic_watchlist(user_id)
                if dynamic_watchlist and dynamic_watchlist.selected:
                    passed_watchlist.update(dynamic_watchlist.symbols)
                if self.progressive_strategy is not None:
                    logger.info("[EMA_PROGRESSIVE] Scanning passed watchlist: %s stocks", len(passed_watchlist))
                elif self.strategy is not None:
                    logger.info("[%s] Scanning passed watchlist: %s stocks", self.strategy.name, len(passed_watchlist))
                for symbol, instrument_token in passed_watchlist.items():
                    if self._stop_event.is_set():
                        break
                    try:
                        candles = self._retry(lambda: self.candle_loader(instrument_token, self.settings.signal_timeframe, 60))
                        if candles is None or candles.empty:
                            continue
                        completed_timestamp = pd.Timestamp(candles.iloc[-1]["timestamp"]).isoformat()
                        key = (user_id, instrument_token, completed_timestamp)
                        if key in self._last_candles:
                            continue
                        symbol_failed = False
                        if self.progressive_strategy is not None:
                            try:
                                self._evaluate_progressive(user_id, symbol, instrument_token, candles)
                            except ValueError as error:
                                if str(error) != "not enough completed candles for EMA 9/200 progressive strategy":
                                    symbol_failed = True
                                    cycle_errors.append(f"{symbol}: {error}")
                                else:
                                    logger.info("[EMA_PROGRESSIVE] Waiting for warm-up candles for %s", symbol)
                            except Exception as error:
                                symbol_failed = True
                                cycle_errors.append(f"{symbol}: {error}")
                                logger.exception("[EMA_PROGRESSIVE] Evaluation failed for %s", symbol)
                        if self.strategy is not None:
                            if isinstance(self.strategy, PreSpikeMomentumStrategy):
                                record = self._evaluate_pre_spike(user_id, symbol, instrument_token, candles)
                                if record is not None:
                                    self._notify_signal(record)
                                    self._log_signal_decision(record)
                                    generated += 1
                            elif not hasattr(self.strategy, "evaluate"):
                                try:
                                    signal = self.strategy.generate_signal(symbol, candles)
                                except NoSignal:
                                    signal = None
                                if signal is not None:
                                    record = SignalRecord(
                                        user_id=user_id,
                                        instrument_token=instrument_token,
                                        symbol=symbol,
                                        side=signal.side,
                                        signal_timestamp=signal.timestamp,
                                        price=signal.price,
                                        stop_loss=signal.stop_loss,
                                        reason=signal.reason,
                                        strategy=self.strategy.name,
                                        score=signal.score,
                                        entry_price=signal.entry_price,
                                        target_1=signal.target_1,
                                        target_2=signal.target_2,
                                        metadata=signal.metadata,
                                    )
                                    if self.repository.save_signal(record):
                                        self._notify_signal(record)
                                        self._log_signal_decision(record, signal=signal)
                                        generated += 1
                            else:
                                try:
                                    evaluation = self.strategy.evaluate(symbol, candles, instrument_token)
                                except NoSignal:
                                    evaluation = None
                                if evaluation is not None:
                                    signal = evaluation.signal
                                    record = SignalRecord(
                                        user_id=user_id,
                                        instrument_token=instrument_token,
                                        symbol=symbol,
                                        side=signal.side,
                                        signal_timestamp=signal.timestamp,
                                        price=signal.price,
                                        vwap=getattr(evaluation, "current_vwap", None),
                                        ema20=getattr(evaluation, "current_ema20", None),
                                        stop_loss=signal.stop_loss,
                                        reason=signal.reason,
                                        strategy=self.strategy.name,
                                        score=signal.score,
                                        entry_price=signal.entry_price,
                                        target_1=signal.target_1,
                                        target_2=signal.target_2,
                                        metadata=signal.metadata,
                                    )
                                    if self.repository.save_signal(record):
                                        self._notify_signal(record)
                                        self._log_signal_decision(record, signal=signal, extra_inputs={"vwap": record.vwap, "ema20": record.ema20})
                                        generated += 1
                        if not symbol_failed:
                            self._last_candles.add(key)
                            self._heartbeat(datetime.now())
                    except Exception as error:
                        if error.__class__.__name__ == "InputException":
                            if "invalid token" in str(error).lower():
                                logger.warning("Signal evaluation skipped for stale instrument %s/%s: %s", user_id, symbol, error)
                                continue
                            cycle_errors.append(f"{symbol}: {error}")
                            logger.warning("Signal evaluation skipped for %s/%s: %s", user_id, symbol, error)
                        else:
                            cycle_errors.append(f"{symbol}: {error}")
                            logger.exception("Signal evaluation failed for %s/%s", user_id, symbol)
            self.last_error = "; ".join(cycle_errors[:3])
        except Exception as error:
            self.last_error = str(error)
            logger.exception("Signal engine cycle failed")
        self.last_run_at = timestamp
        self._save_status(timestamp, self.last_heartbeat_at)
        return generated

    def _notify_signal(self, signal: SignalRecord) -> None:
        notification = NotificationRecord(
            strategy=signal.strategy,
            universe="selected watchlists",
            sector="All sectors",
            symbol=signal.symbol,
            side=signal.side,
            score=signal.score,
            signal_timestamp=signal.signal_timestamp,
            created_at=datetime.now(),
            message="",
            user_id=signal.user_id,
            instrument_token=signal.instrument_token,
        )
        notification = notification.__class__(**{**notification.__dict__, "message": notification_message(notification) + f" reasons={signal.reason}"})
        if self.repository.save_notification(notification):
            self.notifier.send(notification.message)

    def _log_signal_decision(self, record: SignalRecord, signal=None, extra_inputs: dict[str, object] | None = None) -> None:
        """Record every generated signal to the unified decision log, not just the ones acted
        on -- for later strategy review/forecasting, a rejected or unacted-on signal (what the
        scanner saw and how it scored it) is just as informative as one that became a trade.

        `signal` is the richer in-memory `Signal`/evaluation object (score breakdown, risk/reward,
        historical probabilities) when the caller has one; pre-spike's persisted `SignalRecord`
        alone is used otherwise, since that path doesn't produce a comparable object.

        `correlation_id` uses the signal's own symbol+timestamp rather than a later position's
        entry_time, since a signal doesn't know yet whether -- or exactly when -- an order will
        actually fill; TradingPipeline/SwingAutoTrader's own entry decisions aren't logged here
        yet (a natural next step), so this won't auto-join to a resulting trade's rows.
        """
        inputs: dict[str, object] = {"price": record.price, **(extra_inputs or {})}
        if signal is not None:
            for field_name in (
                "risk_reward",
                "expected_value",
                "historical_probability_2_percent",
                "historical_probability_3_percent",
                "target_2_percent",
                "target_3_percent",
            ):
                value = getattr(signal, field_name, None)
                if value is not None:
                    inputs[field_name] = value
            if signal.signal_reasons:
                inputs["signal_reasons"] = list(signal.signal_reasons)
        self.repository.save_decision(
            DecisionLogRecord(
                timestamp=datetime.now(),
                symbol=record.symbol,
                event_type="signal_generated",
                strategy_name=record.strategy,
                mode="SCAN",
                decision=record.side,
                rationale=record.reason,
                inputs=inputs,
                outputs={"stop_loss": record.stop_loss, "target_1": record.target_1, "target_2": record.target_2, "entry_price": record.entry_price},
                confidence=float(record.score) if record.score else None,
                correlation_id=f"{record.symbol}:signal:{record.signal_timestamp.isoformat()}",
                source_table="signals",
                source_id=f"{record.strategy}:{record.instrument_token}:{record.side}:{record.signal_timestamp.isoformat()}",
            )
        )

    def _evaluate_pre_spike(self, user_id: str, symbol: str, instrument_token: int, candles: pd.DataFrame) -> SignalRecord | None:
        strategy = self.strategy
        observation = strategy.inspect(candles)
        timestamp = observation.timestamp
        trading_date = timestamp.date().isoformat()
        event = self.repository.load_latest_pre_spike_event(user_id, strategy.name, symbol, strategy.timeframe)
        if event is not None and event.latest_time == timestamp:
            return None

        if event is not None and event.status == "ACTIVE" and event.trading_date == trading_date:
            self._update_pre_spike_event(event, observation)
            return None

        if event is not None and event.status == "ACTIVE" and event.trading_date != trading_date:
            event = replace(
                event,
                status="COOLDOWN",
                ended_at=timestamp,
                cooldown_until=timestamp,
                latest_time=timestamp,
                latest_price=observation.current_price,
                latest_score=observation.metrics.score,
                latest_metadata=self._pre_spike_metadata(observation),
                reason="Trading session ended.",
            )
            self.repository.save_pre_spike_event(event)

        if event is not None and event.status == "COOLDOWN" and event.cooldown_until and timestamp < event.cooldown_until:
            return None
        if not observation.signal_eligible:
            return None
        if event is not None and not self._is_fresh_pre_spike_trigger(event, observation, trading_date):
            return None

        sequence = self.repository.next_pre_spike_event_sequence(user_id, strategy.name, symbol, strategy.timeframe, trading_date)
        event_id = f"{strategy.name}:{symbol}:{trading_date}:{sequence}"
        signal_key = f"{strategy.name}:{symbol}:{strategy.timeframe}:{timestamp.isoformat()}"
        signal = strategy._signal_from_observation(symbol, observation)
        signal = replace(signal, metadata={**signal.metadata, "event_id": event_id, "signal_key": signal_key})
        event = PreSpikeEventRecord(
            user_id=user_id,
            instrument_token=instrument_token,
            symbol=symbol,
            strategy=strategy.name,
            timeframe=strategy.timeframe,
            trading_date=trading_date,
            event_id=event_id,
            status="ACTIVE",
            trigger_time=timestamp,
            trigger_price=observation.current_price,
            latest_time=timestamp,
            latest_price=observation.current_price,
            latest_score=observation.metrics.score,
            highest_score=observation.metrics.score,
            last_valid_time=timestamp,
            entry_price=observation.current_price,
            latest_metadata=self._pre_spike_metadata(observation),
            reason=signal.reason,
        )
        signal_record = SignalRecord(
            user_id=user_id,
            instrument_token=instrument_token,
            symbol=symbol,
            side=signal.side,
            signal_timestamp=signal.timestamp,
            price=signal.price,
            vwap=observation.metrics.vwap_value,
            ema20=observation.metrics.ema20,
            stop_loss=signal.stop_loss,
            reason=signal.reason,
            strategy=strategy.name,
            score=signal.score,
            entry_price=signal.entry_price,
            target_1=signal.target_1,
            target_2=signal.target_2,
            metadata=signal.metadata,
            event_id=event_id,
            signal_key=signal_key,
        )
        return signal_record if self.repository.save_pre_spike_event_and_signal(event, signal_record) else None

    def _update_pre_spike_event(self, event: PreSpikeEventRecord, observation) -> None:
        invalidation = (
            observation.current_price <= observation.metrics.vwap_value
            and observation.metrics.ema9 <= observation.metrics.ema20
            and observation.current_price < event.entry_price
        )
        invalidation_streak = event.invalidation_streak + 1 if invalidation else 0
        ended = invalidation_streak >= 2
        timestamp = observation.timestamp
        updated = replace(
            event,
            status="COOLDOWN" if ended else "ACTIVE",
            latest_time=timestamp,
            latest_price=observation.current_price,
            latest_score=observation.metrics.score,
            highest_score=max(event.highest_score, observation.metrics.score),
            last_valid_time=timestamp if observation.bullish_direction_valid else event.last_valid_time,
            cooldown_until=timestamp + timedelta(minutes=self._pre_spike_cooldown_minutes()) if ended else None,
            ended_at=timestamp if ended else event.ended_at,
            invalidation_streak=invalidation_streak,
            latest_metadata=self._pre_spike_metadata(observation),
            reason=self.strategy.reason_for_observation(observation),
        )
        self.repository.save_pre_spike_event(updated)

    def _is_fresh_pre_spike_trigger(self, event: PreSpikeEventRecord, observation, trading_date: str) -> bool:
        if event.trading_date != trading_date:
            return True
        previous = event.latest_metadata
        metrics = observation.metrics
        new_day_breakout = metrics.previous_day_breakout and not bool(previous.get("previous_day_breakout"))
        new_twenty_day_breakout = metrics.twenty_day_breakout and not bool(previous.get("twenty_day_breakout"))
        vwap_reclaim = (
            observation.previous_close <= observation.previous_vwap
            and observation.current_price > metrics.vwap_value
            and metrics.rvol >= 3.0
            and metrics.price_change_pct >= 0.50
        )
        ema_reclaim = (
            observation.previous_close <= observation.previous_ema20
            and observation.current_price > metrics.ema20
            and metrics.price_change_pct >= 0.50
        )
        return new_day_breakout or new_twenty_day_breakout or vwap_reclaim or ema_reclaim

    @staticmethod
    def _pre_spike_metadata(observation) -> dict[str, object]:
        metrics = observation.metrics
        return {
            "rvol": metrics.rvol,
            "price_change_pct": metrics.price_change_pct,
            "vwap": metrics.vwap_value,
            "ema9": metrics.ema9,
            "ema20": metrics.ema20,
            "ema50": metrics.ema50,
            "ema200": metrics.ema200,
            "previous_day_high": metrics.previous_day_high,
            "previous_20_day_high": metrics.previous_20_day_high,
            "previous_day_breakout": metrics.previous_day_breakout,
            "twenty_day_breakout": metrics.twenty_day_breakout,
            "volume_buildup_ratio": metrics.volume_buildup_ratio,
            "range_compression": metrics.range_compression,
            "body_ratio": metrics.body_ratio,
            "close_location": metrics.close_location,
            "signal_type": "EARLY_MOMENTUM",
        }

    def _pre_spike_cooldown_minutes(self) -> int:
        return max(0, int(getattr(self.settings, "pre_spike_cooldown_minutes", 30)))

    def _evaluate_progressive(self, user_id: str, symbol: str, instrument_token: int, candles: pd.DataFrame) -> None:
        current_cycle = self.repository.load_latest_progressive_cycle(
            user_id,
            instrument_token,
            self.progressive_strategy.timeframe,
        )
        evaluation = self.progressive_strategy.evaluate(symbol, candles, instrument_token, current_cycle)
        if evaluation.cycle is None:
            return
        cycle = replace(evaluation.cycle, user_id=user_id)
        self.repository.save_progressive_cycle(cycle)
        for event in evaluation.events:
            if event == "LIGHT":
                logger.info("[EMA_PROGRESSIVE] %s -> Fresh EMA9 crossed above EMA200 -> Bucket A / LIGHT", symbol)
            elif event == "STRONG":
                logger.info("[EMA_PROGRESSIVE] %s -> EMA9 > EMA20 > EMA50 > EMA100 -> Bucket B / STRONG", symbol)
            elif event == "INVALIDATED":
                logger.info("[EMA_PROGRESSIVE] %s -> EMA9 <= EMA200 -> Strategy cycle invalidated", symbol)
            elif event == "WEAKENED":
                logger.info("[EMA_PROGRESSIVE] %s -> EMA alignment weakened -> Bucket B / WEAKENED", symbol)

    def response(self, user_id: str | None = None) -> dict[str, object]:
        resolved_user_id = user_id or self.user_id
        if not resolved_user_id:
            raise ValueError("user_id is required for the signal-engine response")
        return self.repository.load_progressive_signal_response(resolved_user_id)

    def _heartbeat(self, timestamp: datetime) -> None:
        self.last_heartbeat_at = timestamp
        poll_seconds = int(getattr(self.settings, "signal_poll_seconds", 30))
        if self._last_persisted_heartbeat_at is None or (timestamp - self._last_persisted_heartbeat_at).total_seconds() >= poll_seconds:
            self._save_status(self.last_run_at or timestamp, timestamp)
            self._last_persisted_heartbeat_at = timestamp

    def _save_status(self, timestamp: datetime, heartbeat_at: datetime | None = None) -> None:
        user_ids = [self.user_id] if self.user_id else self.repository.load_selected_watchlist_users()
        for user_id in user_ids:
            self.repository.save_signal_engine_status(SignalEngineStatus(user_id, timestamp, self.last_error, heartbeat_at or timestamp))

    def _run(self) -> None:
        heartbeat_thread = Thread(target=self._heartbeat_loop, name="signal-engine-heartbeat", daemon=True)
        heartbeat_thread.start()
        try:
            while not self._stop_event.is_set():
                self.run_once()
                self._stop_event.wait(self.settings.signal_poll_seconds)
        finally:
            heartbeat_thread.join(timeout=2)

    def _heartbeat_loop(self) -> None:
        interval = max(5, min(int(getattr(self.settings, "signal_poll_seconds", 30)), 30))
        while not self._stop_event.wait(interval):
            self._heartbeat(datetime.now())

    def _market_open(self, timestamp: datetime) -> bool:
        return self.settings.market_open <= timestamp.time() < self.settings.force_exit

    def _retry(self, operation, attempts: int = 3):
        last_error = None
        for attempt in range(attempts):
            if self._stop_event.is_set():
                return None
            try:
                return operation()
            except Exception as error:
                last_error = error
                if attempt < attempts - 1 and self._stop_event.wait(2**attempt):
                    return None
        raise last_error


def build_signal_engine(settings, access_token: str | None = None, user_id: str | None = None, strategy: Strategy | None = None, legacy_signals_enabled: bool | None = None, enable_progressive_strategy: bool | None = None):
    token = (access_token or settings.kite_access_token.get_secret_value()).strip()
    if not settings.kite_api_key.strip() or not token:
        raise ValueError("KITE_API_KEY and KITE_ACCESS_TOKEN are required for the signal engine")
    require_credentials(settings.kite_api_key, settings.kite_api_secret.get_secret_value())
    broker = KiteClient(settings.kite_api_key, settings.kite_api_secret.get_secret_value())
    broker.connect(AccessToken(token))
    database = Database()
    database.initialize()
    repository = Repository(database)
    if user_id:
        stale_symbols = refresh_selected_watchlist_tokens(repository, user_id, broker.client)
        if stale_symbols:
            logger.warning("Removed stale instruments from selected watchlists: %s", ", ".join(stale_symbols))

    def candle_loader(instrument_token: int, interval: str, lookback_days: int) -> pd.DataFrame:
        end = datetime.now()
        rows = MarketData(broker.client).historical(instrument_token, end - timedelta(days=lookback_days), end, interval)
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        frame = validate_ohlcv(pd.DataFrame(rows).rename(columns={"date": "timestamp"}))
        minute_lengths = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}
        minutes = minute_lengths.get(interval)
        if minutes is None:
            raise ValueError(f"unsupported signal timeframe: {interval}")
        now = pd.Timestamp.now(tz=frame["timestamp"].dt.tz) if frame["timestamp"].dt.tz else pd.Timestamp.now()
        return frame.loc[frame["timestamp"] < now.floor(f"{minutes}min")].reset_index(drop=True)

    if legacy_signals_enabled is None:
        legacy_signals_enabled = strategy is not None
    return AlwaysOnSignalEngine(
        settings,
        repository,
        candle_loader,
        user_id=user_id,
        strategy=strategy,
        legacy_signals_enabled=legacy_signals_enabled,
        enable_progressive_strategy=enable_progressive_strategy,
    ), database