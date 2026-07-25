"""Model registry: adding a model = registering one class here.

The API serves `list_model_schemas()` and the Lab UI renders any model
generically from its schema — zero frontend changes per new model (Phase Plan
hard requirement).
"""

from __future__ import annotations

from quantmind.models.ou import OrnsteinUhlenbeck

_REGISTRY = {m.name: m for m in [OrnsteinUhlenbeck()]}


def get_model(name: str):
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown model {name!r}; registered: {sorted(_REGISTRY)}") from None


def list_model_schemas() -> list[dict]:
    return [m.param_schema() for m in _REGISTRY.values()]
