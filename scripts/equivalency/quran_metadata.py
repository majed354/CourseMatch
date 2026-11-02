"""Offline surah limits from the publisher's unmodified Tanzil metadata.

Source: https://tanzil.net/res/text/metadata/quran-data.xml
Documentation: https://tanzil.net/docs/Quran_Metadata
Copyright (C) 2008-2009 Tanzil.info, license cc-by as declared in XML.
Retrieved 2026-11-03. No network request occurs during comparison.
"""
from pathlib import Path
import hashlib
import xml.etree.ElementTree as ET

SOURCE_URL = 'https://tanzil.net/res/text/metadata/quran-data.xml'
SOURCE_SHA256 = '8867c1d88191472adec9db694b3cd9f135b1a2ef580574d32cf888dcb22c5c7a'
SOURCE_PATH = Path(__file__).parent / 'data' / 'tanzil-quran-data.xml'
_raw = SOURCE_PATH.read_bytes()
if hashlib.sha256(_raw).hexdigest() != SOURCE_SHA256:
    raise ValueError('Quran metadata does not match its recorded publisher checksum')
_chapters = ET.fromstring(_raw).findall('./suras/sura')
if [int(s.attrib['index']) for s in _chapters] != list(range(1, 115)):
    raise ValueError('Quran metadata chapter identities are incomplete')
CHAPTER_AYAS = tuple(int(s.attrib['ayas']) for s in _chapters)
CHAPTER_NAMES = tuple(s.attrib['name'] for s in _chapters)
if sum(CHAPTER_AYAS) != 6236 or not all(n > 0 for n in CHAPTER_AYAS):
    raise ValueError('Quran metadata verse totals are inconsistent')
