#!/usr/bin/env python3
"""
Scrape vacatures voor vacatures-eindhoven GitHub Pages.
Vereisten: pip install playwright requests beautifulsoup4 lxml
           playwright install chromium
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE    = Path(__file__).parent.parent
INDEX   = BASE / 'index.html'
UA      = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
HEADERS = {'User-Agent': UA, 'Accept-Language': 'nl-NL,nl;q=0.9'}

# Per site: URL-patroon waaraan echte vacature-links moeten voldoen
SITE_CONFIG = [
    {
        'name': 'TU/e',
        'org':  'TU/e',
        'url':  'https://www.tue.nl/werken-bij-tue/vacatureoverzicht/',
        # Vacature-URLs bevatten een slug na /vacatureoverzicht/
        'link_pattern': r'tue\.nl/werken-bij-tue/vacatureoverzicht/[a-z]',
        'min_slug_len': 8,   # minimale lengte van de slug na het laatste /
    },
    {
        'name': 'Fontys',
        'org':  'Fontys',
        'url':  'https://werkenbijfontys.nl/fontys/vacatures/',
        # Fontys vacature-URLs: /jobs/functie-titel-NUMMER/
        'link_pattern': r'werkenbijfontys\.nl/jobs/.+-\d{5,}',
        'min_slug_len': 10,
    },
    {
        'name': 'Summa',
        'org':  'Summa',
        'url':  'https://summa-onderwijs.nl/vacatures/',
        # Summa vacature-URLs: /vacatures/specifieke-slug/ (niet de lijstpagina zelf)
        'link_pattern': r'summa-onderwijs\.nl/vacatures/[a-z].+/',
        'min_slug_len': 8,
    },
    {
        'name': 'Gemeente Eindhoven',
        'org':  'Gemeente Eindhoven',
        'url':  'https://www.werkenvooreindhoven.nl/vacature',
        # Gemeente Eindhoven: /vacature/functie-naam
        'link_pattern': r'werkenvooreindhoven\.nl/vacature/[a-z]',
        'min_slug_len': 5,
    },
    {
        'name': 'DBRE',
        'org':  'DBRE',
        'url':  'https://werkenbij.dbre.nl/vacaturebeschrijvingen/actuele-vacatures',
        # DBRE: /vacaturebeschrijvingen/specifieke-vacature (niet de listpagina)
        'link_pattern': r'werkenbij\.dbre\.nl/vacaturebeschrijvingen/(?!actuele-vacatures/?$)',
        'min_slug_len': 3,
    },
]

# Titels die sowieso geen vacature zijn (navigatie, buttons, etc.)
SKIP_TITLES = re.compile(
    r'^(ga naar de inhoud|skip to|fontys|solliciteren|vacancies|vacatures|'
    r'nederlands|english|wij zijn fontys|mensen van fontys|locaties|actueel|'
    r'doe de test|kijk hier voor alle|job alert|meer vacatures laden|'
    r'meer instellingen|werken bij|werken bij summa|home|zoek|zoeken|'
    r'filter|terug|vorige|volgende|sluiten|menu|navigatie|inloggen|'
    r'registreren|contact|over ons|nieuws|events|cookie|privacy|sitemap|'
    r'department of \w+|bachelor|master|phd programmes?|research|education|'
    r'arbeidsmarkt|open application|open sollicitatie)$',
    re.IGNORECASE
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def jsescape(s: str) -> str:
    return (str(s)
            .replace('\\', '\\\\')
            .replace('"', '\\"')
            .replace('\n', ' ')
            .replace('\r', ''))

def clean_text(html_or_text: str, maxlen: int = 200) -> str:
    return BeautifulSoup(html_or_text, 'html.parser').get_text(' ').strip()[:maxlen]

def fmt_date_nl(iso_str: str) -> str:
    try:
        return datetime.fromisoformat(iso_str[:10]).strftime('%-d-%m-%Y')
    except Exception:
        return '–'

def parse_rfc_date(s: str) -> str:
    for fmt in ('%a, %d %b %Y %H:%M:%S %Z', '%a, %d %b %Y %H:%M:%S %z'):
        try:
            return datetime.strptime(s.strip(), fmt).strftime('%-d %b %Y')
        except ValueError:
            continue
    return 'Recent'


# ── JSON-LD helper ────────────────────────────────────────────────────────────

def extract_jsonld_jobs(page) -> list:
    try:
        lds = page.evaluate("""() =>
            [...document.querySelectorAll('script[type="application/ld+json"]')]
                .map(s => { try { return JSON.parse(s.textContent); } catch { return null; } })
                .filter(Boolean)
        """)
    except Exception:
        return []

    jobs = []
    for obj in lds:
        items = obj if isinstance(obj, list) else [obj]
        for o in items:
            if o.get('@type') == 'JobPosting':
                jobs.append(o)
            for g in (o.get('@graph') or []):
                if isinstance(g, dict) and g.get('@type') == 'JobPosting':
                    jobs.append(g)
    return jobs

def parse_jsonld(o: dict, org: str, fallback_url: str) -> dict:
    salary = '–'
    bs = o.get('baseSalary', {})
    if isinstance(bs, dict):
        val = bs.get('value', {})
        if isinstance(val, dict):
            mn, mx = val.get('minValue', ''), val.get('maxValue', '')
            if mn and mx:
                salary = f'€{int(float(mn)):,}–€{int(float(mx)):,}'.replace(',', '.')
            elif mn:
                salary = f'€{int(float(mn)):,}'.replace(',', '.')

    emp_map = {'FULL_TIME': 'Voltijd', 'PART_TIME': 'Parttime',
               'TEMPORARY': 'Tijdelijk', 'CONTRACTOR': 'ZZP', 'INTERN': 'Stage'}
    hours = emp_map.get(o.get('employmentType', ''), '–')

    loc = o.get('jobLocation', {})
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get('address', {}) if isinstance(loc, dict) else {}
    location = addr.get('addressLocality') or addr.get('addressRegion') or 'Eindhoven'

    return {
        'featured': False,
        'title':    o.get('title', '').strip(),
        'org':      org,
        'dept':     (o.get('occupationalCategory') or '').strip() or '–',
        'salary':   salary,
        'hours':    hours,
        'location': location,
        'level':    '–',
        'deadline': fmt_date_nl(o['validThrough']) if o.get('validThrough') else '–',
        'url':      o.get('url', fallback_url),
        'desc':     clean_text(o.get('description', '')),
    }


# ── EDU scraper ───────────────────────────────────────────────────────────────

def scrape_edu_site(pw, cfg: dict) -> list:
    name         = cfg['name']
    org          = cfg['org']
    url          = cfg['url']
    link_pattern = cfg['link_pattern']
    min_slug_len = cfg['min_slug_len']

    print(f'  {name}...')
    browser = pw.chromium.launch(headless=True)
    ctx     = browser.new_context(user_agent=UA, locale='nl-NL')
    page    = ctx.new_page()
    jobs    = []

    try:
        try:
            page.goto(url, wait_until='networkidle', timeout=30000)
        except PWTimeout:
            page.wait_for_timeout(4000)

        # 1. JSON-LD structured data (meest betrouwbaar)
        lds = extract_jsonld_jobs(page)
        if lds:
            jobs = [parse_jsonld(o, org, url) for o in lds if o.get('title')]
            print(f'    ✓ {len(jobs)} via JSON-LD')
            return jobs

        # 2. Site-specifieke URL-patroon matching
        raw = page.evaluate("""(cfg) => {
            const pattern   = new RegExp(cfg.linkPattern);
            const minSlug   = cfg.minSlugLen;
            const seen      = new Set();
            const out       = [];

            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.href || '';
                const text = (a.textContent || '').trim();

                // URL moet overeenkomen met het vacature-patroon
                if (!pattern.test(href)) return;

                // Slug (laatste URL-segment) moet lang genoeg zijn
                const slug = href.replace(/\/$/, '').split('/').pop() || '';
                if (slug.length < minSlug) return;

                // Titel: redelijke lengte
                if (text.length < 8 || text.length > 130) return;

                // Niet in navigatie/header/footer
                if (a.closest('nav, header, footer, [role="navigation"], [aria-label="navigatie"]')) return;

                if (seen.has(href)) return;
                seen.add(href);

                const parent = a.closest('li, article, .card, .job, .vacancy, tr, [class*="job"], [class*="vacanc"]') || a.parentElement;
                const ctx = parent ? parent.innerText.substring(0, 300) : '';
                out.push({ title: text, url: href, ctx });
            });

            return out.slice(0, 60);
        }""", {'linkPattern': link_pattern, 'minSlugLen': min_slug_len})

        # Filter titels die aantoonbaar geen vacature zijn
        raw = [r for r in (raw or []) if not SKIP_TITLES.match(r['title'])]

        if raw:
            for r in raw:
                sal_m = re.search(r'€\s*[\d.,]+(?:\s*[-–]\s*€?\s*[\d.,]+)?', r.get('ctx', ''))
                jobs.append({
                    'featured': False,
                    'title':    r['title'],
                    'org':      org,
                    'dept':     '–',
                    'salary':   sal_m.group().strip() if sal_m else '–',
                    'hours':    '–',
                    'location': 'Eindhoven',
                    'level':    '–',
                    'deadline': '–',
                    'url':      r['url'],
                    'desc':     r.get('ctx', '')[:200],
                })
            print(f'    ✓ {len(jobs)} vacatures')
        else:
            print(f'    ✗ geen vacatures gevonden')

    except Exception as e:
        print(f'    ✗ Fout: {e}')
    finally:
        browser.close()

    return jobs


def scrape_all_edu(pw) -> list:
    all_jobs = []
    for cfg in SITE_CONFIG:
        try:
            jobs = scrape_edu_site(pw, cfg)
            all_jobs.extend(jobs)
        except Exception as e:
            print(f'  ✗ {cfg["name"]}: {e}')
    return all_jobs


# ── Indeed MKT ────────────────────────────────────────────────────────────────

def scrape_indeed() -> list:
    rss = ('https://nl.indeed.com/rss?'
           'q=product+marketeer+OR+product+manager+OR+marketeer+OR+performance+marketeer'
           '&l=Eindhoven&radius=30&sort=date&fromage=30')
    r = requests.get(rss, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, 'xml')
    jobs = []
    for it in soup.find_all('item'):
        raw   = (it.title.text if it.title else '').strip()
        parts = [p.strip() for p in raw.split(' - ')]
        title   = parts[0] or raw
        company = ' - '.join(parts[1:-1]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else '–')
        loc     = parts[-1].split(',')[0].strip() if len(parts) > 1 else 'Eindhoven'
        guid    = it.find('guid')
        url     = guid.text.strip() if guid else ''
        desc    = clean_text(it.description.text if it.description else '')
        posted  = parse_rfc_date(it.pubDate.text if it.pubDate else '')
        jobs.append({
            'title': title, 'company': company, 'location': loc,
            'salary': '–', 'type': 'Fulltime', 'source': 'Indeed',
            'posted': posted, 'url': url, 'desc': desc,
        })
    return jobs


# ── Update index.html ─────────────────────────────────────────────────────────

def jobs_to_js(jobs: list, var: str) -> str:
    lines = [f'const {var} = [']
    for j in jobs:
        parts = []
        for k, v in j.items():
            if isinstance(v, bool):
                parts.append(f'{k}:{str(v).lower()}')
            else:
                parts.append(f'{k}:"{jsescape(v)}"')
        lines.append('  {' + ', '.join(parts) + '},')
    lines.append('];')
    return '\n'.join(lines)

def update_html(mkt: list, edu: list):
    html = INDEX.read_text(encoding='utf-8')

    if mkt:
        html = re.sub(
            r'const MKT = \[.*?\];',
            jobs_to_js(mkt, 'MKT'),
            html, flags=re.DOTALL
        )

    if edu:
        m = re.search(r'const EDU = \[(.*?)\];', html, re.DOTALL)
        if m:
            featured_block = re.findall(
                r'\{[^{}]*?featured\s*:\s*true[^{}]*?\}', m.group(1), re.DOTALL)
            lines = ['const EDU = [']
            for f in featured_block:
                lines.append('  ' + ' '.join(f.split()) + ',')
            for j in edu:
                if not j.get('featured'):
                    parts = []
                    for k, v in j.items():
                        if isinstance(v, bool):
                            parts.append(f'{k}:{str(v).lower()}')
                        else:
                            parts.append(f'{k}:"{jsescape(v)}"')
                    lines.append('  {' + ', '.join(parts) + '},')
            lines.append('];')
            html = re.sub(
                r'const EDU = \[.*?\];',
                '\n'.join(lines),
                html, flags=re.DOTALL
            )

    today = datetime.now().strftime('%-d %B %Y')
    html = re.sub(r'Gegenereerd op [\d\w ]+\d{4}', f'Gegenereerd op {today}', html)
    html = re.sub(r'Basisdata: [\d\w ]+\d{4}',    f'Basisdata: {today}',     html)

    INDEX.write_text(html, encoding='utf-8')
    print(f'\n✓ index.html bijgewerkt ({today})')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print('=== Vacatures scraper ===\n')

    print('Marketing (Indeed):')
    mkt = []
    try:
        mkt = scrape_indeed()
        print(f'  ✓ {len(mkt)} vacatures')
    except Exception as e:
        print(f'  ✗ Fout: {e}')

    print('\nEducatie & Overheid (Playwright):')
    edu = []
    with sync_playwright() as pw:
        edu = scrape_all_edu(pw)

    print(f'\nResultaat: MKT={len(mkt)}, EDU={len(edu)}')
    update_html(mkt or None, edu or None)


if __name__ == '__main__':
    main()
