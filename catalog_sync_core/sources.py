"""Конфигурируемые HTML-адаптеры: поставщики и методы обхода, без каталогов моделей."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from urllib.parse import parse_qs, urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup, NavigableString

from .identity import base_url, clean, extract_brand, extract_model, extract_modification, norm, record_key
from .models import Item
from .valmo_content import extract_valmo
from .prices import parse_price

LOG = logging.getLogger(__name__)
USER_AGENT = 'AkomfortCatalogSync/2.0 (catalog sync; respects robots.txt)'


def jsonld(soup: BeautifulSoup, wanted: str) -> list[dict]:
    found: list[dict] = []
    def walk(obj):
        if isinstance(obj, list):
            for value in obj:
                walk(value)
        elif isinstance(obj, dict):
            types = obj.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if any(str(x).lower() == wanted.lower() for x in types):
                found.append(obj)
            if '@graph' in obj:
                walk(obj['@graph'])
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(node.string or node.get_text()))
        except (TypeError, ValueError):
            continue
    return found


def breadcrumb_path(soup: BeautifulSoup) -> list[str]:
    for item in jsonld(soup, 'BreadcrumbList'):
        elements = item.get('itemListElement', [])
        if not isinstance(elements, list):
            continue
        names = []
        for entry in elements:
            if not isinstance(entry, dict):
                continue
            ref = entry.get('item', {})
            name = entry.get('name') or (ref.get('name') if isinstance(ref, dict) else '')
            if name:
                names.append(clean(name))
        if len(names) > 1:
            return names
    for selector in ('nav[aria-label*=readcrumb]', '[class*=breadcrumb]', '[itemtype*=BreadcrumbList]'):
        node = soup.select_one(selector)
        if not node:
            continue
        names = [clean(n.get_text(' ', strip=True)) for n in node.select('a, [itemprop=name]')]
        names = list(dict.fromkeys(x for x in names if x))
        if len(names) > 1:
            return names
    return []


def parse_specs(soup: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in soup.select('table tr'):
        cells = [clean(x.get_text(' ', strip=True)) for x in row.find_all(['th', 'td'], recursive=False)]
        if len(cells) == 2 and cells[0] and cells[1] and len(cells[0]) <= 90:
            result.setdefault(cells[0], cells[1][:1000])
    for dt in soup.select('dt'):
        dd = dt.find_next_sibling('dd')
        if dd:
            k, v = clean(dt.get_text(' ', strip=True)), clean(dd.get_text(' ', strip=True))
            if k and v and len(k) < 90:
                result.setdefault(k, v[:1000])
    # Частый шаблон сайтов: <div class="param-name">Модификация</div><div>ZZ...</div>
    for node in soup.select('[class*=characteristic], [class*=property], [class*=spec], [class*=param]'):
        children = node.find_all(recursive=False)
        if len(children) == 2:
            k, v = [clean(c.get_text(' ', strip=True)) for c in children]
            if 2 <= len(k) < 85 and v and len(v) < 350 and k != v:
                result.setdefault(k, v)
    return dict(list(result.items())[:120])


def description_and_equipment(soup: BeautifulSoup, product_json: dict) -> tuple[str, list[str]]:
    for selector in ('[itemprop=description]', '[class*=product-description]', '[class*=productDescription]', '#description'):
        node = soup.select_one(selector)
        if node:
            description = clean(node.get_text(' ', strip=True))[:30000]
            if description:
                break
    else:
        description = clean(product_json.get('description', ''))[:30000]
    if not description:
        meta = soup.select_one('meta[name=description]')
        description = clean(meta.get('content', ''))[:30000] if meta else ''
    items: list[str] = []
    for header in soup.find_all(re.compile('^h[2-5]$')):
        if 'КОМПЛЕКТАЦИ' not in norm(header.get_text(' ', strip=True)):
            continue
        sibling = header.find_next_sibling()
        while sibling and sibling.name not in ('h2', 'h3', 'h4') and len(items) < 70:
            for li in sibling.select('li'):
                txt = clean(li.get_text(' ', strip=True))
                if txt and len(txt) < 700:
                    items.append(txt)
            sibling = sibling.find_next_sibling()
        break
    return description, list(dict.fromkeys(items))


def images_from_page(soup: BeautifulSoup, url: str, product_json: dict) -> list[str]:
    """Find actual product photos, not every image from the main page.

    Prefer Product schema.org images; otherwise use only explicit *detail*
    gallery/hero selectors. Never gather `main img` / `[class*=product] img`
    because those also contain recommendations, badges and wheel diagrams.
    """
    structured: list[str] = []
    src = product_json.get('image', [])
    if isinstance(src, (str, dict)):
        src = [src]
    if isinstance(src, list):
        for image in src:
            if isinstance(image, dict):
                image = image.get('contentUrl') or image.get('url')
            if isinstance(image, str):
                structured.append(image)

    # Scope to classes that explicitly identify an image gallery or main image;
    # do not use catch-all slider/card/product selectors.
    gallery_selectors = (
        '[itemprop="image"]',
        '.product-gallery img', '.product__gallery img', '.product-detail__gallery img',
        '.detail-gallery img', '.product-slider img', '.product__slider img',
        '.product-photo img', '.product__photo img', '.product-detail__photo img',
        '.fotorama img', '[data-fancybox="product"] img',
        '[data-fancybox="gallery"] img',
    )
    gallery: list[str] = []
    for node in soup.select(', '.join(gallery_selectors)):
        if node.find_parent(['header', 'footer', 'nav', 'aside']):
            continue
        classes = ' '.join(' '.join(a.get('class', [])) for a in [node] + list(node.parents)[:7]).lower()
        if any(k in classes for k in ('similar', 'related', 'recommend', 'preview', 'catalog-list', 'thumbs-scheme')):
            continue
        parent_link = node.find_parent('a', href=True)
        # Prefer the full original from the product lightbox/zoom over thumbnails.
        for candidate in (node.get('data-zoom'), node.get('data-large'),
                          parent_link.get('href') if parent_link else None,
                          node.get('data-src'), node.get('src')):
            if isinstance(candidate, str) and candidate.strip():
                gallery.append(candidate)
                break

    og: list[str] = []
    node = soup.select_one('meta[property="og:image"]')
    if node and node.get('content'):
        og.append(str(node['content']))
    values = structured or gallery or og
    final: list[str] = []
    for x in values:
        full = urljoin(url, x)
        parsed = urlparse(full)
        path = parsed.path.lower()
        if parsed.scheme != 'https' or not parsed.hostname:
            continue
        if not re.search(r'\.(?:jpe?g|png|webp)$', path, re.I):
            continue
        if any(w in path for w in (
                'logo', 'sprite', 'icon', 'placeholder', 'banner', 'captcha',
                'scheme', 'schema', 'wheel', 'formula', 'kolyes', 'kolec',
                'arrow', 'favicon', '200_200', '100_100',
                'medialibrary')):
            continue
        if '/resize_cache/' in path:
            dims = re.search(r'/(\d{2,4})_(\d{2,4})_\d+/', path)
            # Vendor thumbnail caches are acceptable only at photo-sized
            # resolution, e.g. 780_557_2, never 200_200_1 previews.
            if not dims or int(dims.group(1)) < 480 or int(dims.group(2)) < 320:
                continue
        if full not in final:
            final.append(full)
    return final[:16]




def images_from_valmo_swiper(soup: BeautifulSoup, url: str) -> list[str]:
    """Extract photos strictly from a recognised Valmo product-gallery widget.

    Support Swiper, Slick, Owl, Splide, Fotorama and standard Bitrix galleries.
    Never fall back to arbitrary <img>, og:image or listing/recommendation cards.
    Human review of returned URL list is still mandatory before photo writes.
    """
    prohibited = ('related', 'recommend', 'similar', 'upsell', 'also-buy',
                  'catalog-list', 'catalog-items', 'product-list', 'products-list',
                  'catalog-item', 'product-card', 'product-item-list',
                  'menu', 'banner', 'news', 'brands', 'partners', 'reviews')
    gallery_selector = ('.swiper, .swiper-container, .swiper-holder, .swiper-wrapper, '
                        '.fotorama, .slick-slider, .owl-carousel, .splide, .glide, '
                        '.product-gallery, .product__gallery, .product-detail__gallery, '
                        '.detail-gallery, .product-photos, .product__photos, '
                        '.product-item-detail-slider, .product-slider, .product__slider, '
                        '[data-gallery="product"], [data-fancybox="product"], '
                        '[data-fancybox="gallery"]')
    # Valmo's actual product gallery uses Web Components (<swiper-container> and
    # <swiper-slide>) rather than .swiper-slide classes. Select only the primary
    # product slider; .mySwiper2 contains thumbnails, while .mySwiper123 is a
    # separate promotional carousel (sometimes showing buses).
    primary = soup.select_one('.cardd__slider swiper-container.mySwiper')
    if primary is not None:
        primary_urls: list[str] = []
        for slide in primary.select('swiper-slide'):
            img = slide.find('img')
            if img is None:
                continue
            raw = img.get('src') or img.get('data-src')
            if not raw:
                continue
            full = urljoin(url, raw)
            parsed = urlparse(full)
            if (parsed.scheme == 'https'
                    and parsed.hostname == urlparse(url).hostname
                    and parsed.path.lower().startswith('/upload/iblock/')
                    and re.search(r'\.(?:jpe?g|png|webp)$', parsed.path, re.I)
                    and full not in primary_urls):
                primary_urls.append(full)
        if primary_urls:
            return primary_urls[:16]
    candidates: list[tuple[int, list[str]]] = []
    def image_address(element) -> str | None:
        if not element or not hasattr(element, 'get'):
            return None
        anchor = element if element.name == 'a' else element.find_parent('a', href=True)
        if anchor and not anchor.find_parent(['header', 'footer', 'nav', 'aside']):
            href = anchor.get('href', '')
        else:
            href = ''
        raw_list = [element.get(key) for key in ('data-full', 'data-zoom', 'data-original',
                                                  'data-large', 'data-image', 'data-src')]
        raw_list.extend([href, element.get('src'), element.get('data-lazy'),
                         element.get('data-srcset'), element.get('srcset')])
        style = element.get('style', '')
        if style:
            match = re.search(r'background-image\s*:\s*url\(\s*[\"\']?([^\)\"\']+)', style, re.I)
            if match:
                raw_list.append(match.group(1))
        for raw in raw_list:
            if not isinstance(raw, str) or not raw.strip():
                continue
            # srcset: use the last/largest resolution instead of the thumbnail.
            if ',' in raw and re.search(r'\s\d+[wx](?:\s|,|$)', raw):
                raw = raw.split(',')[-1].strip().split()[0]
            full = urljoin(url, raw.strip())
            parsed = urlparse(full)
            path = parsed.path.casefold()
            if parsed.scheme != 'https' or not parsed.hostname:
                continue
            if parsed.hostname.removeprefix('www.') != urlparse(url).hostname.removeprefix('www.'):
                continue
            if not re.search(r'\.(?:jpe?g|png|webp)$', path):
                continue
            if any(w in path for w in ('logo', 'icon', 'banner', 'placeholder', 'schema',
                                      'scheme', 'wheel', 'arrow', 'sprite', 'favicon')):
                continue
            return full
        return None
    for root in soup.select(gallery_selector):
        if root.find_parent(['header', 'footer', 'nav', 'aside']):
            continue
        if root.find_parent(gallery_selector):
            # Outer gallery is considered once, not again for each slide/anchor.
            continue
        # Reject explicit list/recommendation widgets and their parents.
        ancestors = [root] + list(root.parents)[:7]
        context = ' '.join(' '.join(n.get('class', [])) + ' ' + str(n.get('id', ''))
                           for n in ancestors if hasattr(n, 'get')).casefold()
        if any(part in context for part in prohibited):
            continue
        slides = root.select('.swiper-slide, .slick-slide:not(.slick-cloned), '
                             '.owl-item:not(.cloned), .splide__slide, .glide__slide, '
                             '.fotorama__stage__frame')
        # For a gallery without slide classes, support static <img>, linked
        # thumbnails and data-src lazy images inside an explicit gallery.
        if slides:
            elements = slides
        else:
            raw_elements = ([root] if root.name in ('a', 'img', 'picture', 'source') else
                            root.select('a[href], picture, img, [data-src], [data-background-image]'))
            elements = [e for e in raw_elements
                        if not e.find_parent('a', href=True)
                        and not (e.name == 'img' and e.find_parent('picture'))
                        and not (e.name == 'picture' and e.find_parent('a', href=True))]
            if not elements:
                elements = [root]
        photos: list[str] = []
        for block in elements:
            # Prefer image-specific <a> over nested lazy <img> where possible.
            photo_nodes = ([block] if block.name in ('img', 'source', 'a') else [])
            photo_nodes += block.select('a[href], img, picture source, '
                                         '[data-src], [data-background-image]')
            if not photo_nodes and hasattr(block, 'get'):
                photo_nodes = [block]
            found = None
            for node in photo_nodes:
                # If a <picture> is used, prefer its img/link rather than
                # emitting separate entries for <source> variants.
                found = image_address(node)
                if not found and node.get('data-background-image'):
                    found = image_address(BeautifulSoup(
                        '<img data-src="' + str(node.get('data-background-image')).replace('"', '&quot;') + '">',
                        'html.parser').img)
                if found:
                    break
            if found and found not in photos:
                photos.append(found)
        if not photos:
            continue
        evidence = ('detail', 'gallery', 'photo', 'image', 'product', 'fotorama')
        root_context = ' '.join(root.get('class', [])) + ' ' + str(root.get('id', ''))
        score = 10 if any(x in root_context.casefold() for x in evidence) else 0
        score += min(len(photos), 8)
        # Single-image links can occur in catalog tiles even inside main:
        # require a real gallery widget, or an explicitly labelled product
        # photo link. All results require manual review before applying.
        candidates.append((score, photos))
    return max(candidates, key=lambda c: c[0])[1][:16] if candidates else []


def parse_page(html: str, url: str, source: str, *, path_prefix: str = '/catalog/', id_pattern: str = '', min_product_path_segments: int = 0) -> Item | None:
    soup = BeautifulSoup(html, 'html.parser')
    products = jsonld(soup, 'Product')
    structured = products[0] if products else {}
    h1 = soup.select_one('h1')
    title = clean(structured.get('name') or (h1.get_text(' ', strip=True) if h1 else ''))
    if not title or len(title) > 255:
        return None
    # Valmo H1 frequently contains only brand+model (LiuGong DW90A), while
    # the first content heading says what it actually is (Самосвал LiuGong DW90A).
    # Preserve that category hint to place the item into a Bitrix section.
    if source == 'valmo':
        for h2 in soup.select('h2')[:6]:
            expanded = clean(h2.get_text(' ', strip=True))
            if title.casefold() in expanded.casefold() and 0 < len(expanded) <= 255:
                if len(expanded) > len(title) and not re.search(r'^(технические характеристики|цена|купить)\b', expanded, re.I):
                    title = expanded
                    break
    path = urlparse(url).path.lower().rstrip('/') + '/'
    if not path.startswith(path_prefix.rstrip('/') + '/'):
        return None
    # /catalog/tyagachi/ — раздел, а не товар. Порог задаёт адаптер сайта.
    if min_product_path_segments and len([x for x in path.split('/') if x]) < min_product_path_segments:
        return None
    breadcrumb = breadcrumb_path(soup)
    # Последний breadcrumb часто сам товар, поэтому берём предыдущую позицию.
    if breadcrumb and (norm(title) in norm(breadcrumb[-1]) or norm(breadcrumb[-1]) in norm(title)):
        breadcrumb = breadcrumb[:-1]
    breadcrumb = [x for x in breadcrumb if norm(x) not in ('ГЛАВНАЯ', 'КАТАЛОГ', 'КАТАЛОГ ТОВАРОВ')]
    category = breadcrumb[-1] if breadcrumb else ''
    specs = parse_specs(soup)
    valmo_description = valmo_equipment = valmo_conflicts = None
    if source == 'valmo':
        # Valmo uses <p><span>label</span><span>value</span></p> inside its
        # product-specific characteristics tab, plus free-form technical blocks.
        valmo_specs, valmo_description, valmo_equipment, valmo_conflicts = extract_valmo(soup)
        specs = valmo_specs  # Valmo compact card is untrusted; use narrative/expanded blocks only
    # Отдельное поле GKGross «Модификация» может быть сверстано обычными div.
    if not any('МОДИФИКАЦ' in norm(k) for k in specs):
        page_text = clean(soup.get_text(' ', strip=True))
        match = re.search(r'\bМодификация\s*:?\s*([A-Z]{1,5}\d{4,}[A-Z0-9-]{3,})\b', page_text, re.I)
        if match:
            specs['Модификация'] = match.group(1)
    # Список/раздел не должен попадать в товары только благодаря H1.
    spec_signal = bool(specs) or bool(soup.select_one('[class*=price], [itemprop=price], [class*=product-card]'))
    # Valmo uses a dedicated model page with headings and technical data, often
    # without Product JSON-LD or conventional price HTML. Deep URL required.
    if source == 'valmo' and len([p for p in path.split('/') if p]) >= 4:
        content = soup.get_text(' ', strip=True)
        if re.search(r'Технические\s+характеристики|Характеристики\s+модели', content, re.I):
            spec_signal = True
    if not structured and not spec_signal:
        return None
    if soup.select_one('[class*=catalog-filter]') and not structured and not specs:
        return None
    brand_obj = structured.get('brand') or structured.get('manufacturer') or ''
    brand = brand_obj.get('name', '') if isinstance(brand_obj, dict) else str(brand_obj)
    description, equipment = description_and_equipment(soup, structured)
    if valmo_description is not None:
        description = '\n'.join(valmo_description)[:30000] or description
        equipment = valmo_equipment
    # Valmo calls its chassis/factory index "Модель" (SX4188...), while the
    # catalog model visible to buyers is X6000 from H1. Keep them distinct.
    model_specs = ({k: v for k, v in specs.items() if k.casefold() != 'модель'}
                   if source == 'valmo' else specs)
    model = extract_model(title, model_specs, structured.get('model', '') if isinstance(structured.get('model'), str) else '')
    price_rub, price_is_from = parse_price(soup, source)
    item = Item(source=source, url=url, title=title,
                brand=extract_brand(title, specs, brand), model=model,
                modification=extract_modification(specs, title), category=category,
                category_path=breadcrumb, specs=specs, description=description,
                source_conflicts=valmo_conflicts or [], price_rub=price_rub,
                price_is_from=price_is_from,
                equipment=equipment, images=(images_from_valmo_swiper(soup, url) if source == 'valmo'
                                             else images_from_page(soup, url, structured)),
                source_id=record_key(source, url, id_pattern))
    return item


class Crawler:
    def __init__(self, source_config: dict, *, delay: float = 1.0, timeout: int = 30,
                 max_pages: int = 1500, max_listing_pages: int = 100):
        self.config = source_config
        self.base = source_config['url']
        self.hostname = urlparse(self.base).hostname
        self.delay = delay
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_listing_pages = max_listing_pages
        self.last = 0.0
        self.errors: list[dict] = []
        self.expected_count: int | None = None
        self.discovered_count = 0
        self.warnings: list[str] = []
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.robots = RobotFileParser()
        self.robots.set_url(urljoin(self.base, '/robots.txt'))
        try:
            resp = self.session.get(self.robots.url, timeout=timeout)
            resp.raise_for_status()
            self.robots.parse(resp.text.splitlines())
        except requests.RequestException as exc:
            self.warnings.append(f'{self.hostname}: robots.txt недоступен ({type(exc).__name__})')
            self.robots.disallow_all = True  # нет подтверждения разрешения; запрещаем сбор

    def get(self, url: str) -> requests.Response | None:
        if not self.robots.can_fetch(USER_AGENT, url):
            self.errors.append({'url': url, 'status': 'ROBOTS'})
            return None
        diff = self.delay - (time.monotonic() - self.last)
        if diff > 0:
            time.sleep(diff)
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=False)
            # Only follow HTTPS redirects to the same vendor host (including www.).
            hops = 0
            while r.status_code in (301, 302, 307, 308) and hops < 3:
                location = urljoin(r.url, r.headers.get('Location', ''))
                if urlparse(location).scheme != 'https' or not self.allowed(location) or not self.robots.can_fetch(USER_AGENT, location):
                    break
                r = self.session.get(location, timeout=self.timeout, allow_redirects=False)
                hops += 1
            self.last = time.monotonic()
            if r.status_code in (404, 410):
                self.errors.append({'url': url, 'status': r.status_code})
                return None
            if r.status_code >= 300:
                self.errors.append({'url': url, 'status': r.status_code})
                return None
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            self.errors.append({'url': url, 'status': type(e).__name__})
            return None

    def allowed(self, url: str) -> bool:
        host = urlparse(url).hostname
        if not host or host.removeprefix('www.') != (self.hostname or '').removeprefix('www.'):
            return False
        path = urlparse(url).path.lower().rstrip('/') + '/'
        return not any(path.startswith(str(p).lower().rstrip('/') + '/') for p in self.config.get('excluded_paths', []))

    def sitemap(self) -> list[str]:
        queue = deque([urljoin(self.base, '/sitemap.xml')])
        seen, found = set(), set()
        while queue and len(seen) < 150:
            link = queue.popleft()
            if link in seen or not self.allowed(link):
                continue
            seen.add(link)
            response = self.get(link)
            if response is None:
                continue
            try:
                xml = ElementTree.fromstring(response.content)
            except ElementTree.ParseError:
                self.warnings.append(f'Неверная sitemap: {link}')
                continue
            tag = xml.tag.rsplit('}', 1)[-1]
            urls = [n.text.strip() for n in xml.iter() if n.tag.rsplit('}', 1)[-1] == 'loc' and n.text]
            if tag == 'sitemapindex':
                queue.extend(urls)
            else:
                for u in urls:
                    if self.allowed(u) and urlparse(u).path.startswith(self.config.get('product_prefix', '/catalog/')):
                        found.add(urldefrag(u)[0])
        if queue:
            self.warnings.append('Превышен лимит sitemap')
        if not found:
            self.warnings.append('В sitemap не найдены карточки каталога')
        # Sitemaps sometimes omit part of a catalog; expand category listing links.
        categories = set()
        for link in found:
            segments = [x for x in urlparse(link).path.split('/') if x]
            prefix = self.config.get('product_prefix', '/catalog/').strip('/')
            if prefix and len(segments) >= 2 and segments[0] == prefix:
                categories.add(urljoin(link, '/' + prefix + '/' + segments[1] + '/'))
        max_listings = max(1, min(self.max_listing_pages, 100))
        if len(categories) > max_listings:
            self.warnings.append(f'Ограничение разделов каталога: {len(categories)} > {max_listings}')
        for category in sorted(categories)[:max_listings]:
            response = self.get(category)
            if response is None:
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
            for a in soup.select('a[href]'):
                link = urljoin(category, a['href'])
                if (self.allowed(link) and urlparse(link).path.startswith(urlparse(category).path)
                        and urlparse(link).path.rstrip('/') != urlparse(category).path.rstrip('/')):
                    found.add(urldefrag(link)[0].split('?')[0])
        if len(found) > self.max_pages:
            self.warnings.append(f'Лимит страниц: {len(found)} > {self.max_pages}')
        return sorted(found)[:self.max_pages]

    def catalog_tree(self) -> list[str]:
        """Valmo: walk category pages and their ?PAGEN_1=N pagination.

        Only collect product detail links explicitly marked 'Характеристики и фото'.
        Do not visit arbitrary site URLs or confuse category pages with products.
        """
        root = self.base.rstrip('/') + '/'
        queue = deque([root])
        seen: set[str] = set()
        products: set[str] = set()
        while queue and len(seen) < self.max_listing_pages:
            page_url = queue.popleft()
            if page_url in seen:
                continue
            seen.add(page_url)
            response = self.get(page_url)
            if response is None:
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
            base_path = urlparse(page_url).path.rstrip('/')
            max_pager = 1
            pager_key = ''
            for a in soup.select('a[href]'):
                href = urljoin(page_url, a['href'])
                if not self.allowed(href):
                    continue
                parsed = urlparse(href)
                if not parsed.path.startswith('/catalog/'):
                    continue
                clean_url = urldefrag(href)[0].split('?')[0]
                text = clean(a.get_text(' ', strip=True)).casefold()
                if 'характеристики и фото' in text:
                    products.add(clean_url)
                    continue
                # ?PAGEN_1=2, ?page=2, etc. Keep query for category traversal.
                if parsed.path.rstrip('/') == base_path and parsed.query:
                    params = parse_qs(parsed.query)
                    for k, values in params.items():
                        if re.match(r'^(pagen_\d+|page)$', k, re.I) and values and values[0].isdigit():
                            number = int(values[0])
                            if 2 <= number <= self.max_listing_pages:
                                max_pager = max(max_pager, number)
                                pager_key = k
                                if href not in seen and href not in queue:
                                    queue.append(href)
                    continue
                # Valmo's category tree is /catalog/<brand>/<category>/.
                # Model pages are one level deeper and are collected via CTA.
                depth = len([x for x in parsed.path.split('/') if x])
                if depth <= 3 and parsed.path.rstrip('/') != base_path:
                    if clean_url not in seen and clean_url not in queue:
                        queue.append(clean_url)
            # Pagination might show 1, 2, 5, 7 but not intermediate pages.
            # Follow *every* page in that span, preserving the site's key.
            if pager_key:
                for n in range(2, max_pager + 1):
                    numbered = page_url.split('?')[0] + '?' + pager_key + '=' + str(n)
                    if numbered not in seen and numbered not in queue:
                        queue.append(numbered)
        if queue:
            self.warnings.append('Valmo: лимит страниц разделов/пагинации достигнут; неполный обход')
        if not products:
            self.warnings.append('Valmo: не найдены ссылки на карточки «Характеристики и фото»')
        if len(products) > self.max_pages:
            self.warnings.append(f'Valmo: ограничение карточек {len(products)} > {self.max_pages}')
        return sorted(products)[:self.max_pages]

    def listing(self) -> list[str]:
        root = self.base.rstrip('/') + '/'
        prefix = self.config['product_prefix']
        found: set[str] = set()
        queue = deque([root])
        visited: set[str] = set()
        while queue and len(visited) < self.max_listing_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            response = self.get(url)
            if response is None:
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
            if self.expected_count is None:
                text = soup.get_text(' ', strip=True)
                announced = re.search(r'(?<!\d)(\d+)\s+товар(?:ов|а)?\b', text, re.I)
                if announced:
                    self.expected_count = int(announced.group(1))
            highest = 1
            for a in soup.select('a[href]'):
                full = urljoin(url, a['href'])
                if not self.allowed(full):
                    continue
                parsed = urlparse(full)
                if parsed.path.startswith(prefix):
                    found.add(urldefrag(full)[0].split('?')[0])
                elif parsed.path.rstrip('/') == urlparse(root).path.rstrip('/') and parsed.query:
                    page = parse_qs(parsed.query).get('page', [''])[0]
                    if page.isdigit() and 1 < int(page) <= self.max_listing_pages:
                        highest = max(highest, int(page))
            for page in range(2, highest + 1):
                link = root + '?page=' + str(page)
                if link not in visited and link not in queue:
                    queue.append(link)
        if queue:
            self.warnings.append('Лимит страниц листинга достигнут')
        if not found:
            self.warnings.append('Листинг не содержит карточек')
        if self.expected_count is not None and len(found) < self.expected_count:
            self.warnings.append(f'Листинг обещает {self.expected_count} товаров, найдено ссылок {len(found)}')
        if len(found) > self.max_pages:
            self.warnings.append('Лимит карточек достигнут')
        return sorted(found)[:self.max_pages]

    def crawl(self, *, resume_state: dict | None = None,
              checkpoint_callback=None, checkpoint_every: int = 10) -> list[Item]:
        """Сканирование с возобновлением: уже прочитанные URL не запрашиваются.

        Ошибки 403/429/5xx/сети не помечаются завершёнными и повторяются при
        следующем --resume. 404/410 считаются обработанными, но не товарами.
        """
        if checkpoint_every < 1:
            raise ValueError('checkpoint_every должен быть >= 1')
        previous = resume_state or {}
        urls = previous.get('urls')
        if not isinstance(urls, list) or not urls:
            urls = (self.sitemap() if self.config['mode'] == 'sitemap_catalog'
                    else self.catalog_tree() if self.config['mode'] == 'catalog_tree'
                    else self.listing())
        self.discovered_count = len(urls)
        self.expected_count = previous.get('expected_count', self.expected_count)
        self.warnings = list(dict.fromkeys(self.warnings + [w for w in previous.get('warnings', [])
                                                     if not w.startswith('Не удалось обработать ')]))
        self.errors = list(previous.get('errors', [])) + self.errors
        processed = set(previous.get('processed_urls', []))
        items_by_url = {item.url: item for item in
                        (Item.from_dict(row) for row in previous.get('items', []))}
        attempted = 0

        def save_progress():
            if checkpoint_callback is not None:
                checkpoint_callback({
                    'urls': urls,
                    'processed_urls': sorted(processed),
                    'items': [item.to_dict() for item in items_by_url.values()],
                    'errors': self.errors,
                    'warnings': self.warnings,
                    'expected_count': self.expected_count,
                    'discovered_count': self.discovered_count,
                })

        save_progress()  # Сразу сохраняем список найденных ссылок.
        try:
            for i, url in enumerate(urls, 1):
                if url in processed:
                    continue
                attempted += 1
                response = self.get(url)
                if response is None:
                    errors_for_url = [e for e in self.errors if e.get('url') == url]
                    if errors_for_url and str(errors_for_url[-1].get('status')) in ('404', '410'):
                        processed.add(url)
                else:
                    if 'html' in response.headers.get('Content-Type', 'text/html').lower():
                        item = parse_page(response.text, url, self.config['name'],
                                          path_prefix=self.config['product_prefix'],
                                          id_pattern=self.config.get('source_id_pattern', ''),
                                          min_product_path_segments=int(self.config.get('min_product_path_segments', 0)))
                        if item is not None:
                            items_by_url[url] = item
                    processed.add(url)
                    self.errors = [e for e in self.errors if e.get('url') != url]
                if attempted % checkpoint_every == 0:
                    save_progress()
                if i % 50 == 0:
                    LOG.info('%s %d/%d: %d карточек', self.config['name'], i, len(urls), len(items_by_url))
        finally:
            save_progress()  # При ошибке парсера сохраняем последние успешно пройденные URL.
        if self.expected_count is not None and len(items_by_url) < self.expected_count:
            msg = f'Листинг обещает {self.expected_count}, распознано только {len(items_by_url)} товаров'
            if msg not in self.warnings:
                self.warnings.append(msg)
        if len(processed) != len(urls):
            msg = f'Не удалось обработать {len(urls) - len(processed)} URL; можно повторить --resume'
            if msg not in self.warnings:
                self.warnings.append(msg)
        save_progress()
        return list(items_by_url.values())

    @property
    def complete(self) -> bool:
        return not self.warnings and not any(str(e.get('status')) not in ('404', '410') for e in self.errors)
