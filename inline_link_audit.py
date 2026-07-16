#!/usr/bin/env python3
"""Inline Link Coverage Audit - HTML-aware check for 2c.
Finds editorial pages and checks for Viator affiliate links in <p> tags within first 400 words of <main>.
"""
import sys
import os
import re
from pathlib import Path
from html.parser import HTMLParser

SKIP_PAGES = {'about', 'contact', 'privacy', '404', 'index'}

def is_editorial_page(filepath):
    """Check if this is an editorial page (has <main>, not about/contact/privacy)."""
    stem = Path(filepath).stem.lower()
    return stem not in SKIP_PAGES

class MainContentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_main = False
        self.in_p = False
        self.p_blocks = []  # list of (full_html, word_count_before_link, has_viator_link, link_positions)
        self.current_p_html = ""
        self.current_p_text = ""
        self.current_p_word_count = 0
        self.current_p_has_viator = False
        self.current_p_link_positions = []  # word positions where links occur
        self.total_viator_links = 0
        self.tag_stack = []
        
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == 'main' or (tag in ('div','section') and ('main' in attrs_dict.get('class','') or 'main' in attrs_dict.get('id',''))):
            self.in_main = True
        
        if self.in_main and tag == 'p':
            self.in_p = True
            self.current_p_html = ""
            self.current_p_text = ""
            self.current_p_word_count = 0
            self.current_p_has_viator = False
            self.current_p_link_positions = []
        
        if self.in_p:
            self.current_p_html += self._render_tag(tag, attrs)
        
        if tag == 'a' and 'href' in attrs_dict:
            href = attrs_dict['href']
            if self._is_viator_link(href):
                self.total_viator_links += 1
                if self.in_main:
                    if self.in_p:
                        self.current_p_has_viator = True
                        self.current_p_link_positions.append(self.current_p_word_count)
    
    def handle_endtag(self, tag):
        if self.in_main and tag == 'p':
            self.p_blocks.append({
                'html': self.current_p_html,
                'text': self.current_p_text.strip(),
                'words': self.current_p_word_count,
                'has_viator': self.current_p_has_viator,
                'link_positions': self.current_p_link_positions[:]
            })
            self.in_p = False
        
        if tag in ('main', 'div', 'section'):
            # Could be closing main
            if self.in_main:
                # Only close if we matched the opening <main>
                self.in_main = False
        
        if self.in_p:
            self.current_p_html += f'</{tag}>'
    
    def handle_data(self, data):
        if self.in_p:
            self.current_p_html += data
            self.current_p_text += data
            # Count words in this chunk
            words = data.split()
            self.current_p_word_count += len(words)
    
    def handle_startendtag(self, tag, attrs):
        if self.in_p:
            self.current_p_html += self._render_self_closing(tag, attrs)
    
    def _render_tag(self, tag, attrs):
        attr_str = ''.join(f' {k}="{v}"' for k, v in attrs)
        return f'<{tag}{attr_str}>'
    
    def _render_self_closing(self, tag, attrs):
        attr_str = ''.join(f' {k}="{v}"' for k, v in attrs)
        return f'<{tag}{attr_str}/>'
    
    def _is_viator_link(self, href):
        return bool(re.search(r'viator\.com.*/tours/', href))


class BetterParser(HTMLParser):
    """More robust parser that handles <main> detection better."""
    def __init__(self):
        super().__init__()
        self.main_depth = 0
        self.in_main = False
        self.in_p = False
        self.p_blocks = []
        self.total_viator_links = 0
        self.current_p_words = 0
        self.current_p_has_viator = False
        self.current_p_link_word = None  # word position of first viator link in this p
        self.current_p_text_parts = []
        self.current_p_raw = []
        self.data_buf = []
        
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        attrs_lower = {k.lower(): (v.lower() if v else '') for k, v in attrs_dict.items()}
        
        if tag.lower() == 'main' or ('main' in attrs_lower.get('role', '') or 'main' in attrs_lower.get('id', '') or 'main' in attrs_lower.get('class', '')):
            self.main_depth += 1
            self.in_main = True
        
        if self.in_main and tag.lower() == 'p':
            self.in_p = True
            self.current_p_words = 0
            self.current_p_has_viator = False
            self.current_p_link_word = None
            self.current_p_text_parts = []
            self.current_p_raw = [self._render_tag(tag, attrs)]
        
        if self.in_p:
            self.current_p_raw.append(self._render_tag(tag, attrs))
        
        if tag.lower() == 'a':
            href = attrs_dict.get('href', '')
            if self._is_viator(href):
                self.total_viator_links += 1
                if self.in_main and self.in_p and not self.current_p_has_viator:
                    self.current_p_has_viator = True
                    self.current_p_link_word = self.current_p_words
    
    def handle_endtag(self, tag):
        if tag.lower() == 'main':
            self.main_depth -= 1
            if self.main_depth <= 0:
                self.in_main = False
        
        if self.in_main and tag.lower() == 'p':
            text = ' '.join(self.current_p_text_parts)
            self.p_blocks.append({
                'words': self.current_p_words,
                'has_viator': self.current_p_has_viator,
                'link_word': self.current_p_link_word,
                'snippet': text[:100]
            })
            self.in_p = False
        
        if self.in_p:
            self.current_p_raw.append(f'</{tag}>')
    
    def handle_data(self, data):
        if self.in_p:
            self.current_p_text_parts.append(data)
            words = data.split()
            self.current_p_words += len(words)
            self.current_p_raw.append(data)
    
    def _render_tag(self, tag, attrs):
        if attrs:
            attr_str = ''.join(f' {k}="{v}"' for k, v in attrs)
        else:
            attr_str = ''
        return f'<{tag}{attr_str}>'
    
    def _is_viator(self, href):
        return bool(re.search(r'viator\.com', href))


def audit_site(site_path):
    """Run inline link audit on a site."""
    site = Path(site_path)
    if not site.is_dir():
        print(f"ERROR: {site_path} is not a directory")
        return
    
    html_files = list(site.rglob('*.html'))
    editorial = [f for f in html_files if is_editorial_page(f)]
    
    print(f"\n{'='*70}")
    print(f"Site: {site.name}")
    print(f"Total HTML files: {len(html_files)} | Editorial pages: {len(editorial)}")
    print(f"{'='*70}")
    
    flagged = []
    viator_pages = []
    no_viator_pages = []
    
    for fpath in sorted(editorial):
        try:
            content = fpath.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            print(f"  ⚠️  Cannot read {fpath.relative_to(site)}: {e}")
            continue
        
        parser = BetterParser()
        try:
            parser.feed(content)
        except Exception as e:
            print(f"  ⚠️  Parse error in {fpath.relative_to(site)}: {e}")
            continue
        
        rel = str(fpath.relative_to(site))
        
        if parser.total_viator_links == 0:
            no_viator_pages.append(rel)
            continue
        
        viator_pages.append(rel)
        
        # Check inline coverage within first 400 words
        inline_viator_in_first_400 = False
        first_link_word = None
        word_count = 0
        
        for block in parser.p_blocks:
            word_count += block['words']
            if block['has_viator']:
                link_word = block['link_word']
                cumulative_before = word_count - block['words'] + link_word
                if cumulative_before < 400:
                    inline_viator_in_first_400 = True
                    if first_link_word is None:
                        first_link_word = cumulative_before
                if first_link_word is None:
                    first_link_word = cumulative_before
        
        if not inline_viator_in_first_400:
            flagged.append({
                'file': rel,
                'total_links': parser.total_viator_links,
                'first_link_word': first_link_word,
                'p_blocks': len(parser.p_blocks)
            })
    
    # Report
    print(f"\n📊 Pages with Viator links: {len(viator_pages)}")
    print(f"📊 Pages without Viator links: {len(no_viator_pages)}")
    print(f"\n🔴 Flagged (no inline Viator link in first 400 words of <main>): {len(flagged)}")
    
    for item in flagged:
        fw = f", first link at word ~{item['first_link_word']}" if item['first_link_word'] else ", NO inline viator link found"
        print(f"  📄 {item['file']}")
        print(f"     Total viator links: {item['total_links']}{fw}")
    
    if not flagged and viator_pages:
        print("  ✅ All pages with Viator links have inline coverage in first 400 words!")
    
    return {
        'site': site.name,
        'editorial': len(editorial),
        'viator_pages': len(viator_pages),
        'no_viator': len(no_viator_pages),
        'flagged': flagged
    }


def main():
    results = []
    for site_path in sys.argv[1:]:
        r = audit_site(site_path)
        if r:
            results.append(r)
    
    # Summary
    print(f"\n\n{'='*70}")
    print("SUMMARY - Inline Link Coverage (2c)")
    print(f"{'='*70}")
    total_flagged = sum(len(r['flagged']) for r in results)
    print(f"Total sites: {len(results)}")
    print(f"Total flagged pages: {total_flagged}")
    for r in results:
        print(f"  {r['site']}: {len(r['flagged'])} flagged / {r['viator_pages']} viator pages")

if __name__ == '__main__':
    main()
