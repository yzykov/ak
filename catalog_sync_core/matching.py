"""Безопасное глобальное 1:1 сопоставление модификаций (не только совпадений названий)."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from .identity import configuration, norm
from .families import family_of
from .taxonomy import section_score
from .models import Decision, Item

try:
    from rapidfuzz.fuzz import ratio as fuzzy_ratio
except ImportError:
    fuzzy_ratio = lambda a, b: SequenceMatcher(None, a, b).ratio() * 100

CONFIG_FIELDS = ('variant_suffix', 'modification', 'fuel', 'wheel', 'power_hp', 'gearbox', 'body_m3', 'tire', 'retarder', 'visor', 'adr', 'pto', 'suspension', 'gearbox_variant')


@dataclass
class Evidence:
    differences: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    equal: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return not self.differences and not self.unknown


def same_base(a: Item, b: Item) -> bool:
    """Поиск базы по смыслу машины, а не по ID раздела поставщика/Битрикс.

    В старом catalog_migrate.py использовались family+brand+model, с
    запасным вариантом через заводскую модификацию, если модель не указана.
    Нельзя считать самосвал и тягач одной моделью только из-за бренда C7H.
    """
    fa, fb = family_of(a), family_of(b)
    if not fa or fa != fb or fa == 'parts':
        return False
    if not a.brand or not b.brand or norm(a.brand) != norm(b.brand):
        return False
    ac, bc = configuration(a), configuration(b)
    if a.model and b.model:
        if norm(a.model) != norm(b.model):
            return False
    elif not (ac['modification'] and bc['modification'] and
              ac['modification'] == bc['modification']):
        return False
    if ac['wheel'] and bc['wheel'] and ac['wheel'] != bc['wheel']:
        return False
    return True


# Для идентификации техники эти признаки намного надёжнее, чем длина
# названия или присутствие дополнительного текста в карточке Битрикс.
_IDENTIFIER_WEIGHTS = {
    'modification': 36, 'wheel': 18, 'gearbox': 10, 'body_m3': 10,
    'fuel': 7, 'retarder': 7, 'adr': 7, 'variant_suffix': 5,
    'power_hp': 4, 'suspension': 4, 'pto': 4, 'visor': 3,
    'gearbox_variant': 3, 'tire': 2,
}


def strong_identity(a: Item, b: Item, ev: Evidence) -> bool:
    """Достаточно ли известных признаков для признания существующей карточки.

    Не требуем заполнения всех полей: если в АКомфорт нет мощности,
    это не опровергает совпадение заводского кода/шасси/КПП. Но одного
    названия модели (например «Тягач C7H») недостаточно.
    """
    if ev.differences or not same_base(a, b):
        return False
    eq = set(ev.equal)
    # Для экскаваторов/погрузчиков/грейдеров и другой штучной спецтехники
    # заводская модель обычно обозначает самостоятельную товарную позицию.
    # Для тягачей/самосвалов/шасси одной модели категорически недостаточно.
    model_is_sku_families = {
        'excavator', 'wheel_loader', 'skid_steer', 'grader', 'backhoe',
        'roller', 'truck_crane',
    }
    if (family_of(a) in model_is_sku_families and a.model and b.model
            and norm(a.model) == norm(b.model)
            and float(fuzzy_ratio(norm(a.title), norm(b.title))) >= 52):
        return True
    # Заводской код + колёсная формула + хотя бы один признак комплектации.
    if {'modification', 'wheel'} <= eq and eq.intersection({
            'gearbox', 'body_m3', 'fuel', 'retarder', 'adr',
            'power_hp', 'tire', 'suspension', 'pto'}):
        return True
    # Не каждый поставщик указывает заводской код. Здесь требуется
    # почти дословное название и не менее ДВУХ явных совпадений.
    title_similarity = float(fuzzy_ratio(norm(a.title), norm(b.title)))
    return (title_similarity >= 95 and len(eq) >= 2 and
            bool(eq.intersection({'wheel', 'gearbox', 'body_m3'})))


def candidate_score(a: Item, b: Item, ev: Evidence) -> float:
    """Оценка конкретной комплектации. Fuzzy — лишь небольшой бонус."""
    title = float(fuzzy_ratio(norm(a.title), norm(b.title)))
    eq = set(ev.equal)
    score = 16.0 + 0.25 * title + sum(_IDENTIFIER_WEIGHTS.get(k, 2) for k in eq)
    score -= 18 * len(ev.differences)
    score -= 0.9 * len(ev.unknown)
    if strong_identity(a, b, ev):
        # У штучной спецтехники уникальный индекс модели сам является
        # сильным доказательством. Оценка 36/100 при одинаковой CDM6016
        # вводила в заблуждение, хотя товары уже корректно сопоставлены.
        score = max(score, 89.0 + min(10.0, title * .10))
    return round(max(0.0, min(100.0, score)), 1)


def _edge_weight(a: Item, b: Item, ev: Evidence) -> int:
    """Глобальное назначение: исключаем абстрактные карточки без варианта."""
    if not strong_identity(a, b, ev):
        return 0
    ratio = float(fuzzy_ratio(norm(a.title), norm(b.title)))
    # Заводской код и реальные параметры сильнее близости рекламного H1.
    value = 100_000 + int(200 * ratio)
    value += sum(_IDENTIFIER_WEIGHTS.get(k, 2) * 1000 for k in ev.equal)
    value -= len(ev.unknown) * 500
    # Не отдаём детальную карточку общему H1 без модификации/колёс/КПП.
    return max(1, value)


def _ambiguous_assignment(source_index: int, target_index: int,
                          edges: dict[tuple[int, int], int],
                          assignments: dict[int, int]) -> bool:
    """Если два варианта претендуют на один и тот же SKU, это ручная проверка.

    Нельзя помечать один из них keep только потому, что венгерский алгоритм
    произвольно выбрал победителя между почти одинаковыми кандидатами.
    """
    chosen = edges.get((source_index, target_index), 0)
    competitors = sorted((v for (si, tj), v in edges.items()
                          if tj == target_index and si != source_index
                          and assignments.get(si, target_index) == target_index), reverse=True)
    return bool(competitors and chosen - competitors[0] < 4500)


def compare(a: Item, b: Item) -> Evidence:
    ac, bc = configuration(a), configuration(b)
    result = Evidence()
    for k in CONFIG_FIELDS:
        av, bv = ac[k], bc[k]
        if k == 'variant_suffix' and av != bv:
            result.differences.append(f"{k}: {av or 'BASE'} != {bv or 'BASE'}")
        elif av and bv:
            if av != bv:
                result.differences.append(f'{k}: {av} != {bv}')
            else:
                result.equal.append(k)
        elif av or bv:
            result.unknown.append(f'{k}: {av or "?"} / {bv or "?"}')
    return result


def _hungarian_max(weights: list[list[int]]) -> dict[int, int]:
    """Максимальный вес, назначение строк столбцам; строк <= столбцов.

    Прямоугольная венгерская схема, без scipy/networkx. Столбцы-пустышки
    означают законный отказ от сопоставления конкретной строки.
    """
    n, m = len(weights), len(weights[0]) if weights else 0
    if not n:
        return {}
    assert n <= m
    largest = max(max(row) for row in weights)
    u, v, p, way = [0] * (n + 1), [0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv, used = [float('inf')] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta, j1 = float('inf'), 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = largest - weights[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return {p[j] - 1: j - 1 for j in range(1, m + 1) if p[j]}



def global_assign(n_source: int, n_target: int, edges: dict[tuple[int, int], int]) -> dict[int, int]:
    src, tgt = defaultdict(set), defaultdict(set)
    for (i, j) in edges:
        src[i].add(j)
        tgt[j].add(i)
    done: set[int] = set()
    chosen: dict[int, int] = {}
    for seed in sorted(src):
        if seed in done:
            continue
        S, T = {seed}, set()
        q = deque([('s', seed)])
        while q:
            kind, index = q.popleft()
            if kind == 's':
                done.add(index)
                for j in src[index] - T:
                    T.add(j)
                    q.append(('t', j))
            else:
                for i in tgt[index] - S:
                    S.add(i)
                    q.append(('s', i))
        sl, tl = sorted(S), sorted(T)
        swap = len(sl) > len(tl)
        left, right = (tl, sl) if swap else (sl, tl)
        weights = []
        for i in left:
            row = [edges.get((j, i) if swap else (i, j), -10**12) for j in right]
            row.extend([0] * len(left))
            weights.append(row)
        assignment = _hungarian_max(weights)
        for i, j in assignment.items():
            if j >= len(right) or weights[i][j] <= 0:
                continue
            si, ti = (right[j], left[i]) if swap else (left[i], right[j])
            chosen[si] = ti
    assert len(chosen) == len(set(chosen.values()))
    return chosen


@dataclass
class SyncPlan:
    create: list[Decision] = field(default_factory=list)
    activate: list[Decision] = field(default_factory=list)
    deactivate: list[Decision] = field(default_factory=list)
    upload_images: list[Decision] = field(default_factory=list)
    keep: list[Decision] = field(default_factory=list)
    review: list[Decision] = field(default_factory=list)
    protected: list[Decision] = field(default_factory=list)
    candidates: list[Decision] = field(default_factory=list)  # диагностические пары, не операции записи
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_to_write: bool = False

    def dump(self) -> dict:
        groups = ('create', 'activate', 'deactivate', 'upload_images', 'keep', 'review', 'protected')
        return {'safe_to_write': self.safe_to_write, 'errors': self.errors,
                'warnings': self.warnings,
                'counts': {group: len(getattr(self, group)) for group in groups},
                **{group: [x.to_dict() for x in getattr(self, group)] for group in groups},
                'candidates': [x.to_dict() for x in self.candidates]}


def build_plan(source: list[Item], target: list[Item], taxonomy, *,
               complete: bool, counts: dict[str, int], baseline: dict | None,
               safety: dict, allow_deactivate: bool = False, images_enabled: bool = True) -> SyncPlan:
    plan = SyncPlan()
    if not complete:
        plan.errors.append('Обход одного из источников неполный: запись запрещена')
    if not source or not target:
        plan.errors.append('Пустой каталог')
    src_ids = [(x.source, x.source_id) for x in source]
    if len(set(src_ids)) != len(src_ids):
        plan.errors.append('Дублируются ключи товаров поставщика')
    by_xml = {x.xml_id: i for i, x in enumerate(target) if x.xml_id}
    if len(by_xml) != sum(bool(x.xml_id) for x in target):
        plan.errors.append('В Битрикс дублируются XML_ID')
    # Baseline stores successful counts and is persisted in version control by operator.
    if allow_deactivate and safety.get('require_baseline_for_deactivation', True) and not baseline:
        plan.errors.append('Для деактивации необходим подтверждённый baseline.json')
    if baseline:
        for key, n in counts.items():
            old = (baseline.get('source_counts') or {}).get(key)
            if old and n < float(old) * float(safety.get('min_relative_to_baseline', .85)):
                plan.errors.append(f'Источник {key}: резкое сокращение {old} -> {n}')
    unresolved = [x for x in source if not x.brand or not x.model or not x.section_id]
    if unresolved and not safety.get('allow_missing_catalog_categories', False):
        plan.errors.append(f'Не распознано {len(unresolved)} товаров/разделов: запись заблокирована')
    if allow_deactivate and unresolved:
        plan.errors.append('Неопознанные товары источников: массовое отключение небезопасно')

    # Ambiguous source-side duplicates cannot be silently created as separate cards.
    ambiguous_sources: set[int] = set()
    for i, a in enumerate(source):
        for j in range(i + 1, len(source)):
            b = source[j]
            if a.source == b.source or not same_base(a, b):
                continue
            ev = compare(a, b)
            if not ev.differences:
                ambiguous_sources.update((i, j))
    edges: dict[tuple[int, int], int] = {}
    possible: dict[int, list[int]] = defaultdict(list)
    evidence: dict[tuple[int, int], Evidence] = {}
    for i, s in enumerate(source):
        for j, t in enumerate(target):
            if not same_base(s, t):
                continue
            ev = compare(s, t)
            evidence[i, j] = ev
            possible[i].append(j)
            if ev.differences:
                continue
            weight = _edge_weight(s, t, ev)
            # Идентичный XML_ID — подтверждённая привязка к источнику,
            # но конфликт явных характеристик по-прежнему запрещает запись.
            if t.xml_id == f'{s.source}:{s.source_id}':
                weight += 10_000_000
            if weight > 0:
                edges[i, j] = weight
    assignments = global_assign(len(source), len(target), edges)
    used: set[int] = set()
    import_prefixes = tuple(s.source + ':' for s in source)

    def ranked_for(i: int) -> list[int]:
        a = source[i]
        return sorted(possible.get(i, []),
                      key=lambda j: (not evidence[i, j].differences,
                                     strong_identity(a, target[j], evidence[i, j]),
                                     candidate_score(a, target[j], evidence[i, j]),
                                     len(evidence[i, j].equal),
                                     -len(evidence[i, j].unknown)),
                      reverse=True)

    # Старая сверка показывала подходящие пары даже при недостатке сведений.
    # Восстанавливаем этот диагностический результат отдельно от write-plan.
    for i, s in enumerate(source):
        for j in ranked_for(i)[:3]:
            t, ev = target[j], evidence[i, j]
            parts = []
            if ev.equal:
                parts.append('Совпадают: ' + ', '.join(ev.equal))
            if ev.unknown:
                parts.append('Не указано: ' + '; '.join(ev.unknown))
            if ev.differences:
                parts.append('Различается: ' + '; '.join(ev.differences))
            if assignments.get(i) == j:
                parts.insert(0, 'Назначенная пара 1:1 (только для анализа)')
            plan.candidates.append(Decision('candidate', source=s.source, source_id=s.source_id,
                                            source_url=s.url, title=s.title, element_id=t.bitrix_id,
                                            expected_name=t.title, score=candidate_score(s, t, ev),
                                            reason=' | '.join(parts)))

    def review_with_candidate(i: int, reason: str, j: int | None = None) -> None:
        s = source[i]
        decision = dict(source=s.source, source_id=s.source_id, source_url=s.url, title=s.title)
        if j is not None:
            t = target[j]
            ev = evidence.get((i, j)) or compare(s, t)
            used.add(j)
            detail = []
            if ev.unknown:
                detail.append('неизвестно: ' + '; '.join(ev.unknown))
            if ev.differences:
                detail.append('различия: ' + '; '.join(ev.differences))
            if detail:
                reason += '; ' + '; '.join(detail)
            reason += f'; кандидат АКомфорт #{t.bitrix_id}: {t.title}'
            plan.review.append(Decision('review', **decision, element_id=t.bitrix_id,
                                        expected_name=t.title, score=candidate_score(s, t, ev),
                                        reason=reason))
        else:
            plan.review.append(Decision('review', **decision, reason=reason))

    for i, s in enumerate(source):
        ext = f'{s.source}:{s.source_id}'
        linked_idx = by_xml.get(ext)
        mapped = assignments.get(i)
        ranked = ranked_for(i)
        # Для человека показываем сильнейшую фактическую пару, не общую
        # модель без комплектации. Назначение 1:1 — отдельное свойство.
        best = ranked[0] if ranked else None
        if i in ambiguous_sources:
            review_with_candidate(i, 'Возможно, один товар представлен у другого поставщика', best)
            if linked_idx is not None:
                used.add(linked_idx)
            continue

        if linked_idx is not None:
            linked = target[linked_idx]
            used.add(linked_idx)
            if not same_base(s, linked):
                review_with_candidate(i, 'XML_ID существует, но отличаются тип техники/бренд/модель/колёсная формула', linked_idx)
                continue
            ev = compare(s, linked)
            if ev.differences:
                review_with_candidate(i, 'XML_ID существует, но комплектация отличается', linked_idx)
                continue
            # Привязка источника имеет приоритет перед подбором по названию:
            # на Битрикс могли оставить несколько аналогичных карточек.
            if mapped is not None and mapped != linked_idx:
                used.add(mapped)
                review_with_candidate(i, 'XML_ID и предлагаемое сопоставление расходятся', linked_idx)
                continue
            mapped = linked_idx

        if mapped is not None:
            t = target[mapped]
            used.add(mapped)
            if t.xml_id and t.xml_id.startswith(import_prefixes) and t.xml_id != ext:
                review_with_candidate(i, 'Карточка закреплена за другим исходным URL/XML_ID', mapped)
                continue
            ev = evidence.get((i, mapped)) or compare(s, t)
            if ev.differences:
                review_with_candidate(i, 'Явные отличия комплектации', mapped)
                continue
            if linked_idx is None and _ambiguous_assignment(i, mapped, edges, assignments):
                review_with_candidate(i, 'На одну карточку АКомфорт претендуют несколько комплектаций; нельзя выбрать автоматически', best)
                continue
            confident = (linked_idx == mapped or strong_identity(s, t, ev))
            if confident and t.active:
                # KEEP — только подтверждение существования, не операция записи.
                # Неуказанные на одной стороне поля записываются как примечание,
                # но не делают уже найденный артикул «новым товаром».
                reason = ('Совпадает XML_ID источника' if linked_idx == mapped else
                          'Совпали заводской код/модель и признаки комплектации')
                if ev.unknown:
                    reason += '; не заполнены отдельные поля: ' + '; '.join(ev.unknown)
                plan.keep.append(Decision('keep', source=s.source, source_id=s.source_id,
                                          source_url=s.url, title=s.title, element_id=t.bitrix_id,
                                          expected_name=t.title, expected_xml_id=t.xml_id,
                                          expected_active=t.active,
                                          score=candidate_score(s, t, ev), reason=reason))
                # Загрузку фото не разрешаем на основании только частичного совпадения.
                if images_enabled and not (t.preview_picture or t.detail_picture) and ev.verified:
                    plan.upload_images.append(Decision('upload_images', source=s.source, source_id=s.source_id,
                                                       source_url=s.url, title=s.title, element_id=t.bitrix_id,
                                                       expected_xml_id=t.xml_id, expected_name=t.title))
                continue
            if confident and not t.active and (linked_idx == mapped or ev.verified):
                plan.activate.append(Decision('activate', source=s.source, source_id=s.source_id,
                                              source_url=s.url, title=s.title, element_id=t.bitrix_id,
                                              expected_name=t.title, expected_xml_id=t.xml_id,
                                              expected_active=t.active, score=candidate_score(s, t, ev),
                                              reason='Подтверждённое соответствие неактивной карточке'))
                if images_enabled and not (t.preview_picture or t.detail_picture) and ev.verified:
                    plan.upload_images.append(Decision('upload_images', source=s.source, source_id=s.source_id,
                                                       source_url=s.url, title=s.title, element_id=t.bitrix_id,
                                                       expected_xml_id=t.xml_id, expected_name=t.title))
                continue
            if confident and not t.active:
                review_with_candidate(i, 'Существующая неактивная карточка: активация требует подтверждения недостающих полей', mapped)
                continue
            review_with_candidate(i, 'Близкая карточка без достаточных идентификаторов комплектации', best)
            continue

        # Отсутствие раздела Битрикс НЕ мешает найти существующий товар,
        # но мешает безопасно создать новый. Проверяем после сопоставления.
        if not s.section_id or not s.brand or (not s.model and not s.modification):
            reasons = []
            if not s.section_id:
                reasons.append('Не определён раздел для СОЗДАНИЯ')
            if not s.brand:
                reasons.append('Не определён бренд')
            if not s.model and not s.modification:
                reasons.append('Не определены модель и заводская модификация')
            review_with_candidate(i, '; '.join(reasons), best)
            continue

        # Возможные аналоги не объявляем новыми, пока варианты не проверены.
        if ranked and any(not evidence[i, j].differences for j in ranked):
            review_with_candidate(i, 'Есть похожая карточка, но недостаточно сведений для уверенного сопоставления 1:1', best)
            continue
        if ranked and any(evidence[i, j].unknown for j in ranked):
            review_with_candidate(i, 'Есть близкая комплектация с неполными характеристиками', best)
            continue
        unknown = [t for t in target if (not t.brand or not t.model)
                   and (family_of(s) == family_of(t))
                   and (norm(s.brand) in norm(t.title) or norm(s.model) in norm(t.title))]
        if unknown:
            review_with_candidate(i, 'Есть потенциальный дубль с неполными данными Битрикс')
            continue
        if linked_idx is not None:
            review_with_candidate(i, 'XML_ID привязан к несопоставленной карточке', linked_idx)
        else:
            plan.create.append(Decision('create', source=s.source, source_id=s.source_id,
                                        source_url=s.url, title=s.title, section_id=s.section_id,
                                        reason='Новая комплектация' if ranked else 'Отсутствует карточка'))

    for j, t in enumerate(target):
        if j in used or not t.active:
            continue
        if t.xml_id and t.xml_id not in by_xml:
            plan.review.append(Decision('review', title=t.title, element_id=t.bitrix_id, reason='Невалидный XML_ID'))
            continue
        if (not complete or unresolved or not t.brand or not t.model or
                any(family_of(s) == family_of(t) and (not s.brand or not s.model) for s in source)):
            reason = ('Нельзя доказать отсутствие: неполный обход или нераспознанные товары источников'
                      if not complete or unresolved else 'Недостаточно информации для доказательства отсутствия')
            plan.protected.append(Decision('protected', title=t.title, element_id=t.bitrix_id,
                                           reason=reason))
            continue
        if any(same_base(s, t) and not compare(s, t).differences for s in source):
            plan.review.append(Decision('review', title=t.title, element_id=t.bitrix_id,
                                        reason='Неоднозначное/занятое соответствие'))
            continue
        plan.deactivate.append(Decision('deactivate', element_id=t.bitrix_id, title=t.title,
                                        expected_xml_id=t.xml_id, expected_active=True,
                                        reason='Нет подходящей комплектации у поставщиков'))
    if allow_deactivate:
        if plan.protected:
            plan.errors.append('Есть защищённые товары с недоказанным отсутствием: массовая деактивация запрещена')
        if not plan.keep and any(t.active for t in target):
            plan.errors.append('Нет ни одного подтверждённого совпадения: деактивация запрещена')
        if plan.review and safety.get('require_clean_review_for_deactivation', True):
            plan.errors.append('Есть спорные товары: массовая деактивация запрещена')
        active = sum(t.active for t in target)
        max_deact = int(safety.get('max_deactivate', 5))
        pct = float(safety.get('max_deactivate_percent', 5))
        if len(plan.deactivate) > max_deact or (active and len(plan.deactivate) / active * 100 > pct):
            plan.errors.append(f'Массовая деактивация превышает лимит {max_deact} товаров / {pct}%')
    plan.safe_to_write = not plan.errors
    return plan
