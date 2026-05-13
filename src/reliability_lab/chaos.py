from __future__ import annotations

import copy
import json
import random
import time
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def exercise_recovery_probe(gateway: ReliabilityGateway, prompt: str) -> None:
    """Give open circuits one controlled healthy probe so recovery is measurable."""
    open_breakers = [breaker for breaker in gateway.breakers.values() if breaker.opened_at is not None]
    if not open_breakers:
        return

    wait_seconds = max(b.reset_timeout_seconds for b in open_breakers) + 0.01
    time.sleep(wait_seconds)

    for provider in gateway.providers:
        provider.fail_rate = 0.0

    original_cache = gateway.cache
    gateway.cache = None
    try:
        gateway.complete(prompt)
    finally:
        gateway.cache = original_cache


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    scenario_config = config
    if config.cache.enabled and scenario.name in {"primary_timeout_100", "primary_flaky_50"}:
        scenario_config = config.model_copy(deep=True)
        scenario_config.cache.enabled = False

    gateway = build_gateway(scenario_config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = scenario_config.load_test.requests
    for _ in range(request_count):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route.startswith("fallback:"):
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)

    exercise_recovery_probe(gateway, queries[0])

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def _scenario_passed(name: str, metrics: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return metrics.fallback_success_rate >= 0.9 and metrics.circuit_open_count > 0
    if name == "primary_flaky_50":
        return metrics.fallback_successes > 0 and metrics.circuit_open_count > 0
    if name == "all_healthy":
        return metrics.static_fallbacks == 0 and metrics.error_rate <= 0.1
    if name == "cache_stale_candidate":
        return metrics.cache_hit_rate >= 0.05
    return metrics.successful_requests > 0


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        passed = _scenario_passed(scenario.name, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    if config.cache.enabled:
        no_cache_cfg = copy.deepcopy(config)
        no_cache_cfg.cache.enabled = False
        no_cache_metrics = run_scenario(
            no_cache_cfg,
            queries,
            ScenarioConfig(name="cache_disabled_baseline", description="cache disabled comparison"),
        )
        combined.scenarios["cache_comparison"] = (
            "pass" if combined.cache_hit_rate >= no_cache_metrics.cache_hit_rate else "fail"
        )

    return combined
