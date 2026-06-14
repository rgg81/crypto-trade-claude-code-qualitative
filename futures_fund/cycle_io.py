from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


def cycle_dir(state_dir, cycle_no: int) -> Path:
    return Path(state_dir) / "cycle" / str(cycle_no)


def save_output(state_dir, cycle_no: int, name: str, data: dict | BaseModel) -> Path:
    """Persist an agent's output JSON under state/cycle/<n>/<name>.json.

    The write is ATOMIC (temp file in the same dir + os.replace) so a concurrent reader — notably
    the due-gate scanning report.json — never sees a half-written file: it finds either the prior
    contents or the complete new contents, never a truncated middle."""
    d = cycle_dir(state_dir, cycle_no)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    text = data.model_dump_json(indent=2) if isinstance(data, BaseModel) \
        else json.dumps(data, indent=2, default=str)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, p)
    return p


def load_output(state_dir, cycle_no: int, name: str) -> dict:
    p = cycle_dir(state_dir, cycle_no) / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"no cycle output: {p}")
    return json.loads(p.read_text())


def validate_output(data: dict, model: type[M]) -> M:
    """Validate a raw agent output dict against its contract.

    Raises ValidationError if malformed.
    """
    return model.model_validate(data)
