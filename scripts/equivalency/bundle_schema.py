"""Portable evidence-first generation constraints for categorical bundles.

This returns a grammar copy, never transforms model evidence. Selected structural
contradictions are excluded here. Literal quotes, complete parts, substantive
support, whole-curriculum scope and verse bounds remain independently validated.
Strict is a whole-target 80% band, not a requirement for every detail to match;
a mixed-evidence non-decisive branch preserves that distinction. Lenient proves
the overlapping >=60 band while allowing unresolved strict possibility.
"""
from __future__ import annotations

import copy

_SUPPORTS = ('aligned', 'related', 'limited', 'absent', 'unknown')
_COMMON = ('target_id', 'facets', 'reason')
_ORDERS = {'proposal': _COMMON + ('band', 'possible_tiers', 'uncertainty'),
           'review': _COMMON + ('verdict', 'possible_tiers', 'uncertainty')}


def model_schema(stage, base_schema):
    """Return categorical row branches using only portable anyOf/enum/bounds.

    Unknown never becomes absent. Non-decisive uncertainty remains available to
    lenient/weak readers; empty uncertain bounds remain expressible only for
    independently proven hard exclusions. Evidence property insertion order is
    preserved before the band/verdict; JSON Schema does not mandate output order.
    """
    if stage not in _ORDERS:
        raise ValueError('bundle_schema_stage_invalid')
    result = copy.deepcopy(base_schema)
    key = 'evaluations' if stage == 'proposal' else 'reviews'
    classification = 'band' if stage == 'proposal' else 'verdict'
    try:
        row = result['properties'][key]['items']
        properties = row['properties']
        facets = properties['facets']
        facet = facets['items']
        if (row.get('type') != 'object' or set(properties) != set(_ORDERS[stage])
                or facets.get('type') != 'array' or facet.get('type') != 'object'
                or not {'part_id', 'support', 'source_quotes', 'reason'} <= set(facet['properties'])
                or set(facet['properties']['support']['enum']) != set(_SUPPORTS)):
            raise ValueError('bundle_schema_shape_invalid')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('bundle_schema_shape_invalid') from error

    def array(supports, minimum=1, maximum=None):
        value = copy.deepcopy(facets)
        value['minItems'] = max(value.get('minItems', 1), minimum)
        if maximum is not None:
            value['maxItems'] = min(value.get('maxItems', maximum), maximum)
        item=value['items']
        order=('part_id','source_quotes','reason','support')
        item['properties']={name:item['properties'][name] for name in order}
        item['required']=list(order)
        item['properties']['support']['enum'] = [s for s in _SUPPORTS if s in supports]
        return value

    branches = []
    for band, uncertainty, supports, singleton, choices in (
            ('strict', 'none', ('aligned',), None, [['strict', 'lenient']]),
            ('strict', 'non_decisive', _SUPPORTS, None, [['strict', 'lenient']]),
            ('lenient', 'none', _SUPPORTS[:-1], ('related',), [['lenient']]),
            ('lenient', 'non_decisive', _SUPPORTS, ('related',),
             [['lenient'], ['strict','lenient'], ['lenient','strict']]),
            ('weak', 'none', _SUPPORTS[:-1], ('limited', 'absent'), [[]]),
            ('weak', 'non_decisive', _SUPPORTS, ('limited', 'absent'), [[]]),
            ('uncertain', 'decisive', _SUPPORTS, None,
             [[], ['lenient'], ['strict', 'lenient'], ['lenient', 'strict']])):
        value = copy.deepcopy(row)
        value['properties'] = {name:copy.deepcopy(properties[name]) for name in _ORDERS[stage]}
        value['required'] = list(_ORDERS[stage])
        value['additionalProperties'] = False
        # No contains: singleton contradictions are excluded, while multi-part
        # substantive evidence remains the independent validator's decision.
        value['properties']['facets'] = (array(supports) if singleton is None else
            {'anyOf':[array(singleton,1,1),array(supports,2)]})
        if band=='strict' and uncertainty=='non_decisive':
            value['properties']['facets'] = array(supports,2)
        value['properties'][classification]['enum'] = [band]
        value['properties']['uncertainty']['enum'] = [uncertainty]
        tiers = value['properties']['possible_tiers']
        tiers['enum'] = choices
        tiers['minItems'] = min(map(len,choices))
        tiers['maxItems'] = max(map(len,choices))
        branches.append(value)
    result['properties'][key]['items'] = {'anyOf':branches}
    return result
