"""Shared-allowlist static catalog projection; never mutate canonical input."""
from __future__ import annotations
import json
import math
from pathlib import Path

SCHEMA = json.loads(Path(__file__).with_name('public_catalog_schema.json').read_text())


def _project(value, rule):
    if isinstance(rule, list):
        if not isinstance(value, list):
            raise ValueError('invalid_public_catalog_array')
        return [_project(item, rule[0]) for item in value]
    if isinstance(rule, dict):
        if value is None and rule.get('nullable'):
            return None
        return _project(value, rule['ref'])
    if rule == 'scalar':
        if value is not None and (type(value) not in (str, int, float, bool) or isinstance(value, float) and not math.isfinite(value)):
            raise ValueError('invalid_public_catalog_scalar')
        return value
    if not isinstance(value, dict):
        raise ValueError('invalid_public_catalog_object')
    return {key: _project(value[key], child) for key, child in SCHEMA['definitions'][rule].items() if key in value}


def project_public_catalog(catalog):
    return _project(catalog, SCHEMA['root'])


def exact_json_equal(left, right):
    """JSON numbers compare numerically, but booleans never equal 0/1."""
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isfinite(left) and math.isfinite(right) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(exact_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(exact_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def matches_public_catalog(catalog, published):
    return exact_json_equal(project_public_catalog(catalog), published)
