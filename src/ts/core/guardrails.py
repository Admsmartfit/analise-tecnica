"""Trader guardrails (``config/strategy_default.yaml``) and approved overrides (``config/strategy.override.yaml``).

Dual config (PRD v2.1, adjusted version):

* ``strategy.yaml`` — the trader's base parameters, versioned in git (never written by the system).
* ``strategy_default.yaml`` — guardrails: min/max per parameter, frozen parameters and the re-optimization grid.
  Versioned and never written by the system. Every effective configuration is validated against it, and a
  configuration that violates it does not load (ConfigError), whether it came from a human or from the
  re-optimizer.
* ``strategy.override.yaml`` — local, git-ignored. Written only when the trader approves a candidate. It stores
  just the changed parameters and the hash of the base it was approved on; if the base changes later the override
  is ignored (the trader's new base wins) and the routines alert.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ts.core.errors import ConfigError

logger = logging.getLogger("ts.core.guardrails")

LIMITS_FILE = "strategy_default.yaml"
OVERRIDE_FILE = "strategy.override.yaml"


@dataclass(frozen=True)
class Limits:
    bounds: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    frozen: tuple[str, ...] = ()
    grid: dict[str, list[Any]] = field(default_factory=dict)
    min_trades: int = 60


def get_path(d: dict[str, Any], path: str) -> Any:
    cur: Any = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(path)
        cur = cur[p]
    return cur


def set_path(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def load_limits(config_dir: Path) -> Limits | None:
    p = config_dir / LIMITS_FILE
    if not p.exists():
        return None
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"Error parsing YAML in {p}: {e}") from e
    bounds: dict[str, tuple[float | None, float | None]] = {}
    for path, b in (raw.get("limits") or {}).items():
        if not isinstance(b, dict):
            raise ConfigError(f"{LIMITS_FILE}: limite de '{path}' deve ter min/max")
        bounds[path] = (b.get("min"), b.get("max"))
    reopt = raw.get("reoptimization") or {}
    return Limits(bounds=bounds, frozen=tuple(raw.get("frozen") or ()), grid=dict(reopt.get("grid") or {}),
                  min_trades=int(reopt.get("min_trades", 60)))


def check_limits(cfg_dict: dict[str, Any], limits: Limits) -> list[str]:
    """Human-readable violations (empty list = OK)."""
    out: list[str] = []
    for path, (lo, hi) in limits.bounds.items():
        try:
            v = get_path(cfg_dict, path)
        except KeyError:
            out.append(f"{path}: parâmetro inexistente na configuração")
            continue
        if lo is not None and v < lo:
            out.append(f"{path} = {v} abaixo do mínimo {lo}")
        if hi is not None and v > hi:
            out.append(f"{path} = {v} acima do máximo {hi}")
    return out


def is_frozen(path: str, limits: Limits) -> bool:
    return any(path == f or path.startswith(f + ".") for f in limits.frozen)


def load_override(config_dir: Path) -> dict[str, Any] | None:
    p = config_dir / OVERRIDE_FILE
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or None
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"Error parsing YAML in {p}: {e}") from e


@dataclass
class EffectiveConfig:
    raw: dict[str, Any]
    base_hash: str
    override: dict[str, Any] | None
    override_status: str  # none | applied | stale
    violations: list[str]


def apply_override(base_raw: dict[str, Any], base_hash: str, override: dict[str, Any] | None,
                   limits: Limits | None) -> EffectiveConfig:
    if not override:
        return EffectiveConfig(base_raw, base_hash, None, "none", [])
    if override.get("base_hash") != base_hash:
        logger.warning("strategy.override.yaml was approved on base %s but the base is now %s: ignored",
                       override.get("base_hash"), base_hash)
        return EffectiveConfig(base_raw, base_hash, override, "stale", [])
    eff = copy.deepcopy(base_raw)
    violations: list[str] = []
    for path, value in (override.get("params") or {}).items():
        if limits is not None and is_frozen(path, limits):
            violations.append(f"{path} é congelado em {LIMITS_FILE} e não pode ser alterado")
            continue
        set_path(eff, path, value)
    return EffectiveConfig(eff, base_hash, override, "applied", violations)


def diff_params(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> dict[str, tuple[Any, Any]]:
    """Dotted paths whose value differs between two nested dicts."""
    out: dict[str, tuple[Any, Any]] = {}
    for k in sorted(set(old) | set(new)):
        p = f"{prefix}{k}"
        a, b = old.get(k), new.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            out.update(diff_params(a, b, p + "."))
        elif a != b:
            out[p] = (a, b)
    return out
