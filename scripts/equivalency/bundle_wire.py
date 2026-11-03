"""Lossless source-reference transport for whole-curriculum model evidence.

Anchors are literal source spans, not new topics or weights. The model selects
references; decoding only substitutes the selected exact text and provenance.
Scores, support labels and reasons are neither assigned nor corrected here.
"""
from __future__ import annotations

import copy

from .bundle_evidence import partition_parts
from .bundle_schema import model_schema

VERSION = 'curriculum-bundle-wire-v1-literal-refs'


def source_view(sources):
    """Expose every source once as ordered, contiguous anchors plus a whole ref.

    Joining each source's anchor quotes reconstructs its original effective
    text exactly. The full text is not repeated for the whole reference.
    """
    if not isinstance(sources, list):
        raise ValueError('bundle_wire_sources_not_list')
    result, seen = [], set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('bundle_wire_source_invalid')
        sid = source.get('id')
        text = source.get('corrected_text') or source.get('text')
        if not isinstance(sid, str) or not sid.strip() or sid in seen:
            raise ValueError('bundle_wire_source_identity_invalid')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('bundle_wire_source_text_missing')
        seen.add(sid)
        anchors = [{'ref':f"{sid}:{part['part_id']}", **part}
                   for part in partition_parts(text)]
        cursor = 0
        for anchor in anchors:
            if (anchor['start'] != cursor or anchor['end'] <= cursor
                    or anchor['quote'] != text[anchor['start']:anchor['end']]):
                raise ValueError('bundle_wire_anchor_coverage_invalid')
            cursor = anchor['end']
        if cursor != len(text) or ''.join(a['quote'] for a in anchors) != text:
            raise ValueError('bundle_wire_anchor_coverage_invalid')
        result.append({'source_id':sid, 'whole_ref':f'{sid}:whole', 'anchors':anchors})
    return result


def _references(sources):
    references = {}
    for source in source_view(sources):
        sid = source['source_id']
        for anchor in source['anchors']:
            references[anchor['ref']] = {'source_id':sid, 'quote':anchor['quote']}
        references[source['whole_ref']] = {
            'source_id':sid, 'quote':''.join(a['quote'] for a in source['anchors'])}
    return references


def build_schema(stage, base_schema, sources):
    """Return the consistent model grammar with every literal source ref allowed."""
    if stage not in ('proposal', 'review'):
        raise ValueError('bundle_schema_stage_invalid')
    references = _references(sources)
    if not references:
        raise ValueError('bundle_wire_schema_without_sources')
    result = copy.deepcopy(base_schema)
    key = 'evaluations' if stage == 'proposal' else 'reviews'
    try:
        quotes = result['properties'][key]['items']['properties']['facets']['items']['properties']['source_quotes']
        if quotes.get('type') != 'array':
            raise ValueError('bundle_wire_schema_invalid')
        quotes['items'] = {'type':'string', 'enum':list(references)}
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('bundle_wire_schema_invalid') from error
    return model_schema(stage, result)


def decode_rows(rows, sources):
    """Copy rows and decode exact refs within each facet; reject legacy objects.

    Reusing a reference for another facet/target remains permitted. Duplicate
    citations within a facet, including whole/part aliases of identical text,
    are rejected rather than silently removed or affecting a supplied grade.
    All other domain validation still runs after decoding.
    """
    if not isinstance(rows, list):
        raise ValueError('bundle_wire_rows_not_list')
    references = _references(sources)
    result = copy.deepcopy(rows)
    for row in result:
        if not isinstance(row, dict) or not isinstance(row.get('facets'), list):
            raise ValueError('bundle_wire_row_invalid')
        for facet in row['facets']:
            if not isinstance(facet, dict) or not isinstance(facet.get('source_quotes'), list):
                raise ValueError('bundle_wire_facet_invalid')
            decoded, seen = [], set()
            for ref in facet['source_quotes']:
                if not isinstance(ref, str) or ref not in references:
                    raise ValueError('bundle_wire_foreign_or_nonstring_ref')
                proof = references[ref]
                identity = (proof['source_id'], proof['quote'])
                if identity in seen:
                    raise ValueError('bundle_wire_duplicate_ref')
                seen.add(identity)
                decoded.append(copy.deepcopy(proof))
            facet['source_quotes'] = decoded
    return result
