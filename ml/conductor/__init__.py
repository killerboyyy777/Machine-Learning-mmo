"""Conductor: large-scale agent orchestrator for the Text MMO.

Stage 1 targets: 50 agents, wave startup under 60s, fault isolation,
holdout-gated promotion, market-volume hour soak.

Architecture:
  - Registry: mixed linear+torch agent pool with stable/experimental branches
  - Supervisor: per-task isolation, crash recovery, episode management
  - Churn: Poisson arrivals, geometric lifetimes, wave-based startup
  - Mixer: adaptive cell reallocation across dungeon floors
  - PBT: population-based training (exploit best weights, explore hparams)
  - Metrics: JSONL event stream for offline analysis
"""
from .churn import ChurnManager, geometric_lifetime, poisson_interval, wave_startup
from .conductor import Conductor
from .metrics import MetricsLogger
from .mixer import Mixer
from .pbt import PBTManager
from .registry import AgentEntry, Registry
from .supervisor import AgentTask, Supervisor

__all__ = [
    "AgentEntry",
    "AgentTask",
    "ChurnManager",
    "Conductor",
    "MetricsLogger",
    "Mixer",
    "PBTManager",
    "Registry",
    "Supervisor",
    "geometric_lifetime",
    "poisson_interval",
    "wave_startup",
]
