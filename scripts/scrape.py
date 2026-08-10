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
    # salary
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

    # hours
    emp_map = {'FULL_TIME': 'Voltijd', 'PART_TIME': 'Parttime',
               'TEMPORARY': 'Tijdelijk', 'CONTRACTOR': 'ZZP',
               'INTERN': 'Stage'}
    hours = emp_map.get(o.get('employmentType', ''), '–')

    # location
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


# ── EDU scrapers ──────────────────────────────────────────────────────────────

def scrape_edu_site(pw, name: str, url: str, org: str) -> list:
    print(f'  {name}...')
    browser = pw.chromium.launch(headless=True)
    ctx     = browser.new_context(user_agent=UA, locale='nl-NL')
    page    = ctx.new_page()
    jobs    = []

    try:
        try:
            page.goto(url, wait_until='networkidle', timeout=30000)
        except PWTimeout:
            page.wait_for_timeout(3000)

        # 1. JSON-LD structured data
        lds = extract_jsonld_jobs(page)
        if lds:
            jobs = [parse_jsonld(o, org, url) for o in lds if o.get('title')]
            print(f'    ✓ {len(jobs)} via JSON-LD')
            return jobs

        # 2. Vacancy links uit de DOM
        raw = page.evaluate("""() => {
            const SKIP = /facebook|twitter|linkedin|instagram|youtube|cookie|privacy|sitemap|contact|login|home|zoek|inloggen|registr/i;
            const VAC  = /vacatur|job|opening|werken|functie|positie/i;
            const seen = new Set();
            const out  = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.href || '';
                const text = (a.textContent || '').trim();
                if (text.length < 4 || text.length > 120) return;
                if (SKIP.test(text) || SKIP.test(href)) return;
                if (!VAC.test(href) && !VAC.test(text)) return;
                if (seen.has(href)) return;
                seen.add(href);
                const parent = a.closest('li,article,.card,.job,.vacancy,tr') || a.parentElement;
                const ctx = parent ? parent.innerText.substring(0, 300) : '';
                out.push({ title: text, url: href, ctx });
            });
            return out.slice(0, 50);
        }""")

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
            print(f'    ✓ {len(jobs)} via links')
        else:
            print(f'    ✗ geen vacatures gevonden')

    except Exception as e:
        print(f'    ✗ Fout: {e}')
    finally:
        browser.close()

    return jobs


def scrape_all_edu(pw) -> list:
    sources = [
        ('TU/e',               'https://www.tue.nl/werken-bij-tue/vacatureoverzicht/',              'TU/e'),
        ('Fontys',             'https://werkenbijfontys.nl/fontys/vacatures/',                      'Fontys'),
        ('Summa',              'https://summa-onderwijs.nl/vacatures/',                              'Summa'),
        ('Gemeente Eindhoven', 'https://www.werkenvooreindhoven.nl/vacature',                       'Gemeente Eindhoven'),
        ('DBRE',               'https://werkenbij.dbre.nl/vacaturebeschrijvingen/actuele-vacatures','DBRE'),
    ]
    all_jobs = []
    for name, url, org in sources:
        try:
            jobs = scrape_edu_site(pw, name, url, org)
            all_jobs.extend(jobs)
        except Exception as e:
            print(f'  ✗ {name}: {e}')
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

    # Update MKT
    if mkt:
        html = re.sub(
            r'const MKT = \[.*?\];',
            jobs_to_js(mkt, 'MKT'),
            html, flags=re.DOTALL
        )

    # Update EDU: keep featured items, replace the rest
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

    # Update datums
    today = datetime.now().strftime('%-d %B %Y')
    html = re.sub(r'Gegenereerd op [\d\w ]+\d{4}', f'Gegenereerd op {today}', html)
    html = re.sub(r'Basisdata: [\d\w ]+\d{4}',    f'Basisdata: {today}',     html)

    INDEX.write_text(html, encoding='utf-8')
    print(f'\n✓ index.html bijgewerkt ({today})')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print('=== Vacatures scraper ===\n')

    # MKT
    print('Marketing (Indeed):')
    mkt = []
    try:
        mkt = scrape_indeed()
        print(f'  ✓ {len(mkt)} vacatures')
    except Exception as e:
        print(f'  ✗ Fout: {e}')

    # EDU
    print('\nEducatie & Overheid (Playwright):')
    edu = []
    with sync_playwright() as pw:
        edu = scrape_all_edu(pw)

    # Write
    print(f'\nResultaat: MKT={len(mkt)}, EDU={len(edu)}')
    update_html(mkt or None, edu or None)


if __name__ == '__main__':
    main()
