"""Sanity-check the mock's lognormal latency against the spec's targets.

Appendix B asks for p50~300ms, p90~700ms, p99~1200ms and suggests
random.lognormvariate(mu=5.7, sigma=0.35). This samples 10,000 values per sigma and prints
the percentiles, showing sigma=0.35 undershoots badly and that no single sigma hits both
p90 and p99 targets (0.60 hits p99, 0.66 hits p90).

Usage: python scripts/check_latency.py [--samples N] [--seed S]
"""

import argparse
import math
import random
import statistics

MU = 5.7
SIGMAS = (0.35, 0.60, 0.66)
TARGETS = {"p50": 300, "p90": 700, "p99": 1200}
Z = {"p50": 0.0, "p90": 1.2816, "p99": 2.3263}


def sample_percentiles(mu: float, sigma: float, n: int, seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    cuts = statistics.quantiles((rng.lognormvariate(mu, sigma) for _ in range(n)), n=100)
    return {"p50": cuts[49], "p90": cuts[89], "p99": cuts[98]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"mu={MU}, {args.samples:,} samples per sigma (seed {args.seed})")
    print(f"{'sigma':>6} | {'p50 ms':>7} | {'p90 ms':>7} | {'p99 ms':>7} | analytic p50/p90/p99")
    print(f"{'target':>6} | {TARGETS['p50']:>7} | {TARGETS['p90']:>7} | {TARGETS['p99']:>7} |")
    for sigma in SIGMAS:
        p = sample_percentiles(MU, sigma, args.samples, args.seed)
        analytic = "/".join(f"{math.exp(MU + Z[k] * sigma):.0f}" for k in Z)
        print(
            f"{sigma:>6.2f} | {p['p50']:>7.0f} | {p['p90']:>7.0f} | {p['p99']:>7.0f} | {analytic}"
        )


if __name__ == "__main__":
    main()
