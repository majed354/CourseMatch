"""Literal-reference transport for score-free, source-complete discovery.

This layer only materializes exact original spans selected by reference. It
does not assign retrieval status, join disconnected spans, or alter reasons.
The domain discovery validator still runs after decoding each item.
"""
from __future__ import annotations

import copy

from .bundle_wire import source_view

VERSION = 'candidate-discovery-wire-v1-literal-refs'


def _view(rows, side):
    result = []
    for original in source_view(rows):
        sid = original['source_id']
        # source_view has already validated the exact offsets and coverage.
        # The model only needs ordered refs and text, not redundant offsets/IDs.
        anchors = [{'ref': f'{side}:{part["ref"]}', 'quote': part['quote']} for part in original['anchors']]
        result.append({'id': sid, 'whole_ref': f'{side}:{original["whole_ref"]}', 'anchors': anchors})
    return result


def views(targets, sources):
    """Keep all effective text, ordered once, with distinct side namespaces."""
    return {'target': _view(targets, 'target'), 'source': _view(sources, 'source')}


def _references(rows, side):
    result = {}
    for item in _view(rows, side):
        for anchor in item['anchors']:
            result[anchor['ref']] = {'id': item['id'], 'quote': anchor['quote']}
        result[item['whole_ref']] = {
            'id': item['id'], 'quote': ''.join(a['quote'] for a in item['anchors'])}
    return result


def build_schema(base_schema, targets, sources):
    """Copy the grammar, replacing literal fields by finite, typed-side refs."""
    refs = {'target': _references(targets, 'target'), 'source': _references(sources, 'source')}
    if any(not values for values in refs.values()):
        raise ValueError('candidate_wire_schema_requires_both_curricula')
    result = copy.deepcopy(base_schema)

    def convert(schema):
        if isinstance(schema, dict):
            properties = schema.get('properties')
            if isinstance(properties, dict):
                replaced = {}
                for name, definition in properties.items():
                    if name in ('source_quote', 'target_quote'):
                        side = name.split('_', 1)[0]
                        replaced[side + '_ref'] = {'type': 'string', 'enum': list(refs[side])}
                    else:
                        replaced[name] = definition
                schema['properties'] = replaced
                if isinstance(schema.get('required'), list):
                    schema['required'] = [name.replace('_quote', '_ref') if name in ('source_quote', 'target_quote') else name
                                          for name in schema['required']]
            for value in schema.values():
                convert(value)
        elif isinstance(schema, list):
            for value in schema:
                convert(value)

    convert(result)
    return result


def _quote(ref, owner, references):
    if not isinstance(ref, str) or ref not in references or references[ref]['id'] != owner:
        raise ValueError('candidate_wire_foreign_or_cross_identity_ref')
    return references[ref]['quote']


def _decode_anchor(row, target_id, target_refs, source_refs):
    if not isinstance(row, dict) or set(row) != {'source_id', 'source_ref', 'target_ref'}:
        raise ValueError('candidate_wire_anchor_fields_invalid')
    sid = row['source_id']
    if not isinstance(sid, str):
        raise ValueError('candidate_wire_source_identity_invalid')
    return {'source_id': sid,
            'target_quote': _quote(row['target_ref'], target_id, target_refs),
            'source_quote': _quote(row['source_ref'], sid, source_refs)}


def decode_pair(row, target_id, targets, sources):
    """Decode one pair, allowing the caller to retain other valid scan items."""
    if not isinstance(row, dict):
        raise ValueError('candidate_wire_pair_invalid')
    if row.get('status') == 'unrelated':
        if set(row) != {'source_id', 'status'}:
            raise ValueError('candidate_wire_unrelated_fields_invalid')
        return copy.deepcopy(row)
    if set(row) != {'source_id', 'status', 'reason', 'source_ref', 'target_ref'}:
        raise ValueError('candidate_wire_pair_fields_invalid')
    anchor = _decode_anchor({name: row[name] for name in ('source_id', 'source_ref', 'target_ref')}, target_id,
                            _references(targets, 'target'), _references(sources, 'source'))
    return {**anchor, 'status': copy.deepcopy(row['status']), 'reason': copy.deepcopy(row['reason'])}


def decode_group(row, targets, sources):
    """Decode independent original spans; never create a surrounding interval."""
    if (not isinstance(row, dict) or set(row) != {'target_id', 'source_ids', 'reason', 'evidence'}
            or not isinstance(row['evidence'], list)):
        raise ValueError('candidate_wire_group_fields_invalid')
    target_refs, source_refs = _references(targets, 'target'), _references(sources, 'source')
    result = copy.deepcopy(row)
    result['evidence'] = [_decode_anchor(anchor, row['target_id'], target_refs, source_refs)
                          for anchor in row['evidence']]
    return result


def decode_discovery(value, targets, sources):
    """Strict whole-value adapter for CPU checks; production decodes per item."""
    if (not isinstance(value, dict) or set(value) != {'scan', 'groups'}
            or not isinstance(value['scan'], list) or not isinstance(value['groups'], list)):
        raise ValueError('candidate_wire_discovery_fields_invalid')
    result = copy.deepcopy(value)
    for row in result['scan']:
        if not isinstance(row, dict) or set(row) != {'target_id', 'sources'} or not isinstance(row['sources'], list):
            raise ValueError('candidate_wire_scan_fields_invalid')
        row['sources'] = [decode_pair(pair, row['target_id'], targets, sources) for pair in row['sources']]
    result['groups'] = [decode_group(row, targets, sources) for row in result['groups']]
    return result
