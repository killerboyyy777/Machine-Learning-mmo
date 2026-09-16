"""Conductor: large-scale agent orchestrator for the Text MMO.

Stage 1 targets: 50 agents, wave startup under 60s, fault isolation,
holdout-gated promotion, market-volume hour soak.

Architecture:
  - Registry: mixed linear+torch agent pool with stable/experimental branches
  - Supervisor: per-task isolation, crash recovery, episode management
  - Churn: Poisson arrivals, geometric lifetimes, wave-based startup
  - Mixer: adaptive cell reallocation across dungeon floors
  - Metrics: JSONL event stream for offline analysis
"""
