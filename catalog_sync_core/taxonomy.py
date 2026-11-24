"""Сопоставление реальных разделов Битрикс с категориями сайтов; без ID и моделей в коде."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlsplit, unquote

from .identity import WHEELS, norm
from .models import Item, Section


def is_truck_chassis(title: str, brand: str = '') -> bool:
    text = norm(title + ' ' + brand)
    if not re.search(r'\b(?:ШАССИ|АВТОШАССИ)\b', text):
        return False
    # Small DONGFENG/JUNFENG chassis stay in the original LCV catalog,
    # including if a supplier happens to list them. Do not move them.
    if re.search(r'DONGFENG|ДОНГФЕНГ|JUNFENG|ДЖУНФЕНГ', text):
        return False
    # Excavator/loader track chassis are not truck chassis.
    return not re.search(r'ЭКСКАВАТОР|ПОГРУЗЧИК|ТРАКТОР|КРАН|КОМБАЙН|ПРИЦЕП', text)


def commercial_import_destination(item: Item) -> str:
    """Exactly the two requested new commercial categories.

    Explicit supplier/product evidence only. Native cards are never routed.
    Never infer from marketing prose or the generic word 'commercial'.
    """
    if item.source not in ('valmo', 'gkgross', 'liftnet'):
        return ''
    title = norm(item.title + ' ' + item.category)
    if re.search(r'DONGFENG|ДОНГФЕНГ|JUNFENG|ДЖУНФЕНГ',
                 norm(item.title + ' ' + item.brand)):
        return ''
    if re.search(r'ЗЕРНОВОЗ|ЗЕРНОВЫЙ\s+САМОСВАЛ', title):
        return 'komm-zernovozy'
    path = urlsplit(item.url).path.casefold()
    if (re.search(r'\bАВТОБУС\b|\bАВТОБУСЫ\b|\bАВТОБУСОВ\b', title)
            or (item.source == 'valmo' and re.search(r'/catalog/bus(?:/|$)', path))):
        return 'komm-avtobusy'
    return ''

try:
    from rapidfuzz.fuzz import ratio as fuzzy_ratio
except ImportError:
    fuzzy_ratio = lambda a, b: 100 * SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class Mapping:
    section_id: int
    score: float
    reason: str


def stem_token(token: str) -> str:
    """Неглубокая нормализация склонений, не переводчик и не список категорий."""
    token = norm(token)
    if token.isdigit():
        return token
    for suffix in ('ИЧЕСКИЕ', 'ИЧЕСКАЯ', 'ИЧЕСКИЙ', 'ОВЫЕ', 'ЕВЫЕ', 'СКИЕ', 'СКАЯ', 'СКИЙ',
                   'НЫЕ', 'НАЯ', 'НЫЙ', 'ОВ', 'ЕВ', 'АМИ', 'ЯМИ', 'АХ', 'ЯХ', 'Ы', 'И', 'А', 'Я', 'Е'):
        if len(token) > len(suffix) + 4 and token.endswith(suffix):
            return token[:-len(suffix)]
    return token


def section_signature(name: str) -> str:
    tokens = [stem_token(x) for x in re.findall(r'\w+', norm(name))]
    return ' '.join(sorted(x for x in tokens if x))


def section_score(a: str, b: str) -> float:
    an, bn = section_signature(a), section_signature(b)
    if not an or not bn:
        return 0
    if an == bn:
        return 100
    # Разные числовые подкатегории, напр. 6x4 и 8x4, не объединяются.
    ad, bd = set(re.findall(r'\d+', an)), set(re.findall(r'\d+', bn))
    if ad != bd:
        return 0
    # У поставщика «Тягачи», в Битрикс «Седельные тягачи»:
    # общее смысловое существительное без подмены разных типов техники.
    # 91 ниже точного совпадения (100), но выше порога 90.
    a_tokens, b_tokens = set(an.split()), set(bn.split())
    generic = {'КАТАЛОГ', 'ТЕХНИК', 'АВТОМОБИЛ', 'ТРАНСПОРТ', 'СПЕЦТЕХНИК'}
    shared = (a_tokens & b_tokens) - generic
    if shared and (a_tokens <= b_tokens or b_tokens <= a_tokens):
        return 91.0
    return float(round(fuzzy_ratio(an, bn), 2))


class Taxonomy:
    def __init__(self, rows: list[dict], excluded_codes: list[str], *,
                 minimum_score: float = 90, minimum_margin: float = 9,
                 aliases: dict | None = None):
        self.sections = {int(x['id']): Section(int(x['id']), str(x['name']), str(x['code']),
                                               int(x.get('parent_id') or 0), bool(x.get('active', True))) for x in rows}
        self.excluded_codes = set(excluded_codes)
        self.minimum_score = minimum_score
        self.minimum_margin = minimum_margin
        self.aliases = aliases or {}

    def excluded(self, section_id: int) -> bool:
        visited = set()
        while section_id:
            if section_id in visited or section_id not in self.sections:
                return True
            visited.add(section_id)
            s = self.sections[section_id]
            if s.code.lower() in self.excluded_codes:
                return True
            section_id = s.parent_id
        return False

    def is_descendant(self, child: int, parent: int) -> bool:
        seen = set()
        while child and child in self.sections and child not in seen:
            if child == parent:
                return True
            seen.add(child)
            child = self.sections[child].parent_id
        return False

    def choose(self, item: Item) -> Mapping:
        # User-approved: supplier-imported truck chassis belong to the
        # "Седельные тягачи" section. Never affect native DONGFENG LCV cards.
        if item.source in ('gkgross', 'liftnet', 'valmo') and is_truck_chassis(item.title, item.brand):
            sections = [s for s in self.sections.values()
                        if s.code == 'sedelnye-tyagachi' and s.active and not self.excluded(s.id)]
            if len(sections) == 1:
                return Mapping(sections[0].id, 100, 'Шасси импортера: раздел седельных тягачей')
            return Mapping(0, 0, 'Не найден однозначный раздел sedelnye-tyagachi')
        commercial = commercial_import_destination(item)
        if commercial:
            sections = [sec for sec in self.sections.values()
                        if sec.code == commercial and sec.active and not self.excluded(sec.id)]
            parents = [sec for sec in self.sections.values()
                       if sec.code == 'kommercheskiy-transport' and sec.active]
            if (len(parents) == 1 and len(sections) == 1 and
                    sections[0].parent_id == parents[0].id):
                return Mapping(sections[0].id, 100, 'Импортированный автобус/зерновоз: отдельная категория')
            return Mapping(0, 0, 'Отсутствует/неоднозначна категория ' + commercial)
        # URL-based aliases have precedence over generic breadcrumb/category labels.
        # Example: valmo/catalog/shacman/sedelnye-tyagachi1/ -> sedelnye-tyagachi
        path = unquote(urlsplit(item.url).path).strip('/').casefold()
        source = item.source.strip('/').casefold()
        url_matches = []
        for key, value in self.aliases.items():
            prefix = str(key).strip('/').casefold()
            if prefix.startswith(source + '/'):
                relative = prefix[len(source) + 1:]
                # Trailing '*' explicitly requests a raw URL prefix match.
                # A trailing '-' is also a prefix (e.g. product/lonking-).
                # Other aliases match a complete path segment / directory.
                wildcard = relative.endswith('*')
                if wildcard:
                    relative = relative[:-1]
                if not relative:
                    continue
                if (path.startswith(relative) if (wildcard or relative.endswith('-'))
                        else path == relative or path.startswith(relative + '/')):
                    url_matches.append((len(relative), value))
        alias = None
        if url_matches:
            # Prefer the most specific path; ties with different targets are ambiguous.
            longest = max(n for n, _ in url_matches)
            values = [v for n, v in url_matches if n == longest]
            if len({str(v) for v in values}) != 1:
                return Mapping(0, 0, 'Конфликт правил URL-категорий')
            alias = values[0]
        elif item.category:
            alias = self.aliases.get(item.source + '/' + item.category)
            if alias is None:
                alias = self.aliases.get(item.category)
        if alias is not None:
            candidates = [s for s in self.sections.values()
                          if str(s.id) == str(alias) or s.name == alias or s.code == alias]
            candidates = [s for s in candidates if s.active and not self.excluded(s.id)]
            if len(candidates) == 1:
                return Mapping(candidates[0].id, 100, 'Подтверждённый псевдоним категории')
            return Mapping(0, 0, 'Псевдоним категории неверен или неоднозначен')
        if not item.category:
            return Mapping(0, 0, 'Поставщик не указал категорию товара')
        def score_for_item(section: Section) -> float:
            score = section_score(item.category, section.name)
            # Подраздел «Тягачи 6x4» можно выбрать по колёсной формуле товара,
            # не прописывая в коде конкретные категории и модели.
            section_wheel = WHEELS.search(section.name)
            if section_wheel:
                title_wheel = WHEELS.search(item.title)
                if not title_wheel or norm(title_wheel.group()) != norm(section_wheel.group()):
                    return 0
                bare_section = WHEELS.sub('', section.name).strip(' -/,')
                if section_score(item.category, bare_section) >= self.minimum_score:
                    return max(score, 110.0)
            return score
        scores = sorted(((score_for_item(s), s.id)
                         for s in self.sections.values() if s.active and not self.excluded(s.id)), reverse=True)
        if not scores or scores[0][0] < self.minimum_score:
            return Mapping(0, scores[0][0] if scores else 0, 'Нет уверенного соответствия категории в Битрикс')
        if len(scores) > 1 and scores[0][0] - scores[1][0] < self.minimum_margin:
            return Mapping(0, scores[0][0], 'Несколько разделов Битрикс подходят одинаково')
        return Mapping(scores[0][1], min(100.0, scores[0][0]), 'Категория сопоставлена автоматически')

    def enrich_source(self, items: list[Item]) -> list[tuple[Item, Mapping]]:
        result = []
        for item in items:
            m = self.choose(item)
            item.section_id = m.section_id
            item.section_match_confidence = m.score
            result.append((item, m))
        return result


def find_category_from_title(title: str, sections: list[Section]) -> str:
    """Резерв: ищем формулировку раздела в первых словах имени машины.

    Не используем фиксированные названия категорий и не угадываем по URL.
    """
    beginning = [stem_token(x) for x in re.findall(r"\w+", norm(title))[:6]]
    matches = []
    for section in sections:
        tokens = [stem_token(x) for x in re.findall(r"\w+", norm(section.name))]
        if tokens and len(tokens) <= 4 and all(t in beginning[:max(4, len(tokens) + 1)] for t in tokens):
            matches.append(section.name)
    return matches[0] if len(matches) == 1 else ''
