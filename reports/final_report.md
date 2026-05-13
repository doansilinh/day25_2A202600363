# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway checks a safety-aware response cache first, then routes through a provider fallback chain protected by per-provider circuit breakers. If every provider is unavailable, it returns a static degraded-service response instead of retrying indefinitely.

```text
User -> Gateway -> Cache check -> cache hit
                  -> CircuitBreaker(primary) -> Provider primary
                  -> CircuitBreaker(backup)  -> Provider backup
                  -> Static fallback
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Opens quickly after repeated provider failures without tripping on one transient error. |
| reset_timeout_seconds | 2.0 | Gives the provider a short recovery window before a half-open probe. |
| success_threshold | 1 | One successful probe is enough for this fake local provider lab. |
| cache backend | redis | Redis is used for the submitted config so separate gateway instances share cached responses. |
| cache TTL seconds | 300 | Keeps FAQ-style answers fresh while preserving useful hit rate during the lab run. |
| similarity_threshold | 0.92 | High enough to avoid false hits such as 2024 vs 2026 policy questions. |
| load_test requests | 100 | Enough requests per scenario to trigger cache, fallback, and circuit behavior. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 1.0000 | yes |
| Latency P95 | < 2500 ms | 503.6000 | yes |
| Fallback success rate | >= 95% | 1.0000 | yes |
| Cache hit rate | >= 10% | 0.3675 | yes |
| Recovery time | < 5000 ms | 18424.5777 | no |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 1.0000 |
| error_rate | 0.0000 |
| latency_p50_ms | 218.4400 |
| latency_p95_ms | 503.6000 |
| latency_p99_ms | 539.0800 |
| fallback_success_rate | 1.0000 |
| cache_hit_rate | 0.3675 |
| circuit_open_count | 18 |
| recovery_time_ms | 18424.5777 |
| estimated_cost | 0.1088 |
| estimated_cost_saved | 0.1470 |

## 5. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 250.4100 | 218.4400 | -12.8% |
| latency_p95_ms | 502.8800 | 503.6000 | +0.1% |
| estimated_cost | 0.1888 | 0.1088 | -42.4% |
| cache_hit_rate | 0.0000 | 0.3675 | +0.3675 |

False-hit guardrail checked: `refund policy for 2024` does not satisfy a later `refund policy for 2026` lookup because date-like 4-digit numbers differ.

## 6. Redis shared cache

Redis-backed cache is implemented and covered by `tests/test_redis_cache.py`. Redis was started through Docker in WSL for the final verification run.

Shared cache matters because horizontally scaled gateway instances should reuse the same safe answers instead of each process warming its own memory cache. `SharedRedisCache` stores query/response hashes with TTL, supports exact and similarity lookup, skips privacy-like queries, and logs year/ID false-hit candidates.

Evidence captured with Redis running:

```bash
python -m pytest tests/test_redis_cache.py -q
# 6 passed

wsl docker compose up -d; python -m pytest -q
# 11 passed, 1 xpassed

wsl docker compose exec -T redis redis-cli KEYS "rl:cache:*"
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fails, circuit opens, backup handles traffic. | availability/counters recorded in metrics JSON | pass |
| primary_flaky_50 | Primary is unstable, traffic mixes primary and fallback with circuit openings. | availability/counters recorded in metrics JSON | pass |
| all_healthy | No static fallback and low error rate. | availability/counters recorded in metrics JSON | pass |
| cache_stale_candidate | Cache produces safe hits while avoiding date-sensitive false hits. | availability/counters recorded in metrics JSON | pass |
| cache_comparison | Cache-enabled run has at least the no-cache hit rate. | availability/counters recorded in metrics JSON | pass |

## 8. Failure analysis

The remaining production weakness is that circuit breaker state is still process-local. In a real multi-instance deployment, one hot instance may learn that a provider is failing while another instance keeps sending traffic until its own breaker opens. I would move breaker counters and state transitions to Redis with atomic operations, then add provider-level rate limits.

## 9. Next steps

1. Add Redis-backed circuit state so fallback behavior is consistent across gateway instances.
2. Replace deterministic token similarity with embeddings or a vetted semantic index for low-risk FAQ traffic.
3. Export Prometheus metrics for request count, latency, cache hits, and circuit state.

## 10. Reproducibility

```bash
python -m pytest -q
python -m ruff check src tests scripts
python -m mypy src
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md
```