"""Evidence-first generation constraints; never a response/grade transformer.

This grammar prevents selected structural contradictions. It does not replace
quote, scope, coverage or semantic validators. JSON Schema does not mandate
property emission order; insertion order supplies the grammar's evidence-first
layout. Choosing unknown followed by uncertain is already an allowed verdict,
not a post-generation adjustment of a score or support value.
"""
from __future__ import annotations

import copy


_PROPERTY_ORDER = ('target_id', 'source_id', 'facets', 'reason', 'verdict', 'possible_tiers')
_SUPPORTS = ('aligned', 'related', 'limited', 'absent', 'unknown')


def model_review_schema(base_schema, *, allow_empty_uncertain=False):
    """Return four explicit row branches from the existing word-span schema.

    Only portable anyOf/enum/array bounds express conditional restrictions.
    The caller keeps the original validators, and supplies a schema whose facet
    items already use target_start/target_end rather than generated quotes.
    Neither the base schema nor model responses are mutated. Empty uncertain
    bounds require an explicit batch-level hard verse-exclusion exception; the
    unchanged per-pair validator still verifies that exception for every row.
    """
    if type(allow_empty_uncertain) is not bool:
        raise ValueError('allow_empty_uncertain_must_be_boolean')
    result = copy.deepcopy(base_schema)
    try:
        row = result['properties']['reviews']['items']
        properties = row['properties']
        facets = properties['facets']
        facet = facets['items']
        if (set(properties) != set(_PROPERTY_ORDER) or row.get('type') != 'object'
                or facets.get('type') != 'array' or facet.get('type') != 'object'
                or not {'target_start', 'target_end', 'source_quote', 'support'} <= set(facet['properties'])
                or 'target_quote' in facet['properties']
                or set(facet['properties']['support']['enum']) != set(_SUPPORTS)):
            raise ValueError('word_span_review_schema_required')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('word_span_review_schema_required') from error

    def array(allowed, minimum=1, maximum=None):
        value = copy.deepcopy(facets)
        value['minItems'] = max(value.get('minItems', 1), minimum)
        if maximum is not None:
            value['maxItems'] = min(value.get('maxItems', maximum), maximum)
        value['items']['properties']['support']['enum'] = [support for support in _SUPPORTS if support in allowed]
        return value

    def branch(verdict, allowed, singleton=None):
        value = copy.deepcopy(row)
        value['properties'] = {key: copy.deepcopy(properties[key]) for key in _PROPERTY_ORDER}
        value['required'] = list(_PROPERTY_ORDER)
        value['additionalProperties'] = False
        value['properties']['verdict']['enum'] = [verdict]
        if singleton is None:
            value['properties']['facets'] = array(allowed)
        else:
            value['properties']['facets'] = {'anyOf': [array(singleton, 1, 1), array(allowed, 2)]}
        choices = {'strict': [['strict', 'lenient']], 'lenient': [['lenient']], 'weak': [[]],
                   'uncertain': ([[]] if allow_empty_uncertain else []) + [['lenient'], ['strict', 'lenient'], ['lenient', 'strict']]}[verdict]
        tiers = value['properties']['possible_tiers']
        tiers['enum'] = choices
        tiers['minItems'] = min(map(len, choices))
        tiers['maxItems'] = max(map(len, choices))
        return value

    result['properties']['reviews']['items'] = {'anyOf': [
        branch('strict', ('aligned',)),
        branch('lenient', ('aligned', 'related', 'limited', 'absent'), ('related',)),
        branch('weak', ('aligned', 'related', 'limited', 'absent'), ('limited', 'absent')),
        branch('uncertain', _SUPPORTS, ('unknown',)),
    ]}
    return result
