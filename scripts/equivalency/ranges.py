"""Conservative numeric checks for explicit, chapter-bound Quran verse scopes.

This is an upper-bound guard for 80/60 topic grades, never a semantic score.
Unknown chapters, multiple ranges and unlabelled numbers are left untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from .quran_metadata import CHAPTER_AYAS, CHAPTER_NAMES, SOURCE_URL, SOURCE_SHA256

_NAMES = ('الفاتحة البقرة آل_عمران النساء المائدة الأنعام الأعراف الأنفال التوبة يونس هود يوسف الرعد إبراهيم الحجر النحل الإسراء الكهف مريم طه الأنبياء الحج المؤمنون النور الفرقان الشعراء النمل القصص العنكبوت الروم لقمان السجدة الأحزاب سبأ فاطر يس الصافات ص الزمر غافر فصلت الشورى الزخرف الدخان الجاثية الأحقاف محمد الفتح الحجرات ق الذاريات الطور النجم القمر الرحمن الواقعة الحديد المجادلة الحشر الممتحنة الصف الجمعة المنافقون التغابن الطلاق التحريم الملك القلم الحاقة المعارج نوح الجن المزمل المدثر القيامة الإنسان المرسلات النبأ النازعات عبس التكوير الانفطار المطففين الانشقاق البروج الطارق الأعلى الغاشية الفجر البلد الشمس الليل الضحى الشرح التين العلق القدر البينة الزلزلة العاديات القارعة التكاثر العصر الهمزة الفيل قريش الماعون الكوثر الكافرون النصر المسد الإخلاص الفلق الناس').split()
_NAMES = [name.replace('_', ' ') for name in _NAMES]
assert len(_NAMES) == 114


def normalized(text):
    text = unicodedata.normalize('NFKC', str(text or ''))
    text = re.sub(r'[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u200e\u200fـ]', '', text)
    text = text.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹أإآى', '01234567890123456789اااي'))
    return ' '.join(text.split())


assert tuple(map(normalized, _NAMES)) == tuple(map(normalized, CHAPTER_NAMES))
_ALIASES = {normalized(name): index for index, name in enumerate(_NAMES, 1)}
_ALIASES.update({normalized('فاتحة الكتاب'):1, normalized('أم الكتاب'):1, normalized('براءة'):9,
                 normalized('بني إسرائيل'):17, normalized('بني اسرائيل'):17})
_NAME_PATTERN = '|'.join(re.escape(name).replace(r'\ ', r'\s+') for name in sorted(_ALIASES, key=len, reverse=True))
_NAME = re.compile(r'(?<!\w)(?P<name>' + _NAME_PATTERN + r')(?!\w)')
_EXPLICIT = re.compile(r'(?<!\w)[وبلفك]{0,2}(?:ال)?سورة\s*[(:：]?\s*(?P<name>' + _NAME_PATTERN + r')(?!\w)')
_BARE_TAFSIR = re.compile(r'(?<!\w)تفسير\s+(?:ص(?:فحة)?\s*[:：]?\s*\d+\s+)?(?P<name>' + _NAME_PATTERN + r')(?=\s*[,،:：()]?\s*(?:الايات|ايات|الاية|اية))')
_BARE_START = re.compile(r'^(?P<name>' + _NAME_PATTERN + r')(?=\s*[,،:：()]?\s*(?:الايات|ايات|الاية|اية|المقطع))')
_PRINTED_TAFSIR = re.compile(r'(?<!\w)تفسير\s+(?:ص(?:فحة)?\s*[:：]?\s*\d+\s+)?(?P<name>' + _NAME_PATTERN + r')\s*[(\[]\s*(?P<first>\d{1,3})(?!\d)\s*[-–—−]\s*(?P<last>\d{1,3})(?!\d)\s*[)\]]')
_CHAPTER_CUE = re.compile(r'(?<!\w)[وبلفك]{0,2}(?:ال)?(?:سورة|سورتي|سورتين|سورتا|سور)(?!\w)')
_COORDINATED = re.compile(r'\s*[,،]?\s*و\s*(?:سورة\s*)?(?P<name>' + _NAME_PATTERN + r')(?!\w)')
_NUMBER = r'(?P<start>\d{1,3})(?!\d)\s*(?:[-–—−]\s*|(?:الي|الى)\s*(?:(?:الاية|اية)\s*)?)(?P<end>\d{1,3})(?!\d)'
_VERSE_RANGE = re.compile(r'(?<!\w)[وفبلك]{0,2}(?:الايات|ايات|الاية|اية)\s*[:：]?\s*[(\[]?\s*(?:(?:من|م)\s*)?' + _NUMBER)
_SEGMENT_RANGE = re.compile(r'(?<!\w)[وفبلك]{0,2}المقطع(?P<between>[^\n.;؛]{0,80}?)' + _NUMBER)
_SINGLE_VERSE = re.compile(r'(?<!\w)[وفبلك]{0,2}(?:الاية|اية)\s*[:：]?\s*[(\[]?\s*(?P<number>\d{1,3})(?!\d)(?!\s*[-–—−]\s*\d)')
_NON_VERSE = re.compile(r'(?:اسبوع|اسابيع|حديث|احاديث|ساع|شهر|اشهر|عام|سنة|سنوات|صفحة|ص\s*:|رمز|مقرر|خطة|تاريخ|مستوي|درس)')


@dataclass(frozen=True)
class VerseScope:
    chapter: int
    start: int
    end: int
    chapter_topic_id: str

    @property
    def chapter_name(self):
        return _NAMES[self.chapter - 1]

    def evidence(self):
        return {'chapter':self.chapter, 'chapter_name':self.chapter_name,
                'start':self.start, 'end':self.end, 'chapter_topic_id':self.chapter_topic_id}


def _chapter_in(text):
    matches = list(_EXPLICIT.finditer(text)) + list(_BARE_TAFSIR.finditer(text)) + list(_BARE_START.finditer(text)) + list(_PRINTED_TAFSIR.finditer(text))
    chapters = {_ALIASES[match['name']] for match in matches}
    for match in matches:
        end = match.end()
        while continuation := _COORDINATED.match(text, end):
            chapters.add(_ALIASES[continuation['name']])
            end = continuation.end()
    if len(chapters) == 1:
        return next(iter(chapters)), False
    return None, bool(chapters) or bool(_CHAPTER_CUE.search(text))


def _range_candidates(text, *, check_limits=False):
    # Validation must see invalid/oversized printed endpoints too. The normal
    # scope parser remains limited to plausible verse numbers. Bound conversion
    # avoids Python's huge-integer parsing limit for untrusted manual input.
    def pattern(value):
        return re.compile(value.pattern.replace(r'\d{1,3}', r'\d+')) if check_limits else value
    def number(value):
        return int(value) if len(value) <= 6 else 1000000
    found = []
    for match in pattern(_PRINTED_TAFSIR).finditer(text):
        found.append((match.start('first'), match.end('last'), number(match['first']), number(match['last'])))
    for match in pattern(_VERSE_RANGE).finditer(text):
        found.append((match.start('start'), match.end('end'), number(match['start']), number(match['end'])))
    for match in pattern(_SEGMENT_RANGE).finditer(text):
        if _NON_VERSE.search(match['between']):
            continue
        after = text[match.end():match.end()+18].lstrip(' )]:،,.')
        if _NON_VERSE.search(after):
            continue
        found.append((match.start('start'), match.end('end'), number(match['start']), number(match['end'])))
    for match in pattern(_SINGLE_VERSE).finditer(text):
        if not any(start <= match.start('number') < end for start, end, _, _ in found):
            value = number(match['number'])
            found.append((match.start('number'), match.end('number'), value, value))
    return found


def _range_in(text):
    found = _range_candidates(text)
    bounds = {(min(a,b), max(a,b)) for _, _, a, b in found if 1 <= a <= 286 and 1 <= b <= 286}
    return next(iter(bounds)) if len(bounds) == 1 else None


def invalid_scope_topics(topics):
    """Prove impossible verse endpoints only for an unambiguous chapter.

    Multiple chapter assignments remain unresolved instead of attributing all
    their numbers to the first chapter. Printed pages are never verse limits.
    """
    current = None
    issues = []
    for topic in topics:
        text = normalized(topic.get('corrected_text') or topic.get('text', ''))
        chapter, ambiguous = _chapter_in(text)
        if ambiguous:
            current = None
        elif chapter is not None:
            current = chapter
        if current is None:
            continue
        maximum = CHAPTER_AYAS[current - 1]
        bad = sorted({(a, b) for _, _, a, b in _range_candidates(text, check_limits=True)
                      if not 1 <= a <= maximum or not 1 <= b <= maximum})
        if bad:
            issues.append({'topic_id': topic['id'], 'chapter': current,
                           'chapter_name': _NAMES[current - 1], 'maximum_verse': maximum,
                           'invalid_bounds': [list(bounds) for bounds in bad],
                           'metadata_source': SOURCE_URL, 'metadata_sha256': SOURCE_SHA256})
    return issues


def topic_scopes(topics):
    """Carry only a single confirmed chapter forward, resetting on ambiguity."""
    current = None
    chapter_topic = ''
    result = {}
    for topic in topics:
        text = normalized(topic.get('corrected_text') or topic.get('text', ''))
        chapter, ambiguous = _chapter_in(text)
        if ambiguous:
            current = None
        elif chapter is not None:
            current, chapter_topic = chapter, str(topic['id'])
        bounds = _range_in(text)
        valid = current is not None and bounds and not ambiguous and bounds[1] <= CHAPTER_AYAS[current - 1]
        result[topic['id']] = VerseScope(current,*bounds,chapter_topic) if valid else None
    return result


def range_issues(pairs, target, source):
    targets, sources = topic_scopes(target), topic_scopes(source)
    issues = []
    for pair in pairs:
        score = pair.get('score')
        if type(score) is not int or score < 60:
            continue
        target_scope = targets.get(pair.get('target_id'))
        source_scope = sources.get(pair.get('source_id'))
        if target_scope is None or source_scope is None:
            continue
        same_chapter = target_scope.chapter == source_scope.chapter
        intersection = max(0,min(target_scope.end,source_scope.end)-max(target_scope.start,source_scope.start)+1) if same_chapter else 0
        denominator = target_scope.end-target_scope.start+1
        threshold = 80 if score >= 80 else 60
        if intersection*100 < threshold*denominator:
            issues.append({'field':'verse_scope' if same_chapter else 'surah',
                           'target_id':pair['target_id'],'source_id':pair['source_id'],
                           'score':score,'required_coverage':threshold,
                           'intersection_verses':intersection,'target_verses':denominator,
                           'target_range_coverage':round(100*intersection/denominator,4),
                           'target_scope':target_scope.evidence(),'source_scope':source_scope.evidence()})
    return issues


def required_scope_pairs(target, source):
    """Explicit range alternatives with >=60% target coverage must be assessed.

    Presence is mandatory, not a high grade: depth/content may still score <60.
    General prose and unresolved chapter/range rows create no matrix obligation.
    """
    targets, sources = topic_scopes(target), topic_scopes(source)
    required = []
    for target_id, target_scope in targets.items():
        if target_scope is None:
            continue
        denominator = target_scope.end-target_scope.start+1
        for source_id, source_scope in sources.items():
            if source_scope is None or source_scope.chapter != target_scope.chapter:
                continue
            intersection=max(0,min(target_scope.end,source_scope.end)-max(target_scope.start,source_scope.start)+1)
            if intersection*100 >= 60*denominator:
                required.append({'target_id':target_id,'source_id':source_id,
                                 'intersection_verses':intersection,'target_verses':denominator,
                                 'target_range_coverage':round(100*intersection/denominator,4),
                                 'target_scope':target_scope.evidence(),'source_scope':source_scope.evidence()})
    return required
