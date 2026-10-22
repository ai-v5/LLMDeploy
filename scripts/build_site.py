"""Build only the explicitly listed public Markdown pages; never copy the repo."""
import argparse
import html
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import unicodedata
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def slug(value, separator):
    value = ''.join(c for c in html.unescape(value).lower()
                    if c in '-_ ' or unicodedata.category(c)[0] in 'LN')
    return re.sub(r'\s', separator, value)


def pages(config):
    sources, outputs = set(), set()
    for page in config['pages']:
        source, output = page['source'], page['output']
        for value in (source, output):
            path = PurePosixPath(value)
            if path.is_absolute() or '..' in path.parts or '\\' in value or any(p.startswith('.') for p in path.parts):
                raise ValueError('INVALID_SITE_PATH')
        if source not in ('README.md', 'THIRD_PARTY_NOTICES.md') and not source.startswith('docs/'):
            raise ValueError('SOURCE_OUTSIDE_PUBLIC_DOCUMENTATION')
        if not source.endswith('.md') or not output.endswith('.html') or source in sources or output in outputs:
            raise ValueError('DUPLICATE_OR_INVALID_PAGE')
        sources.add(source)
        outputs.add(output)
    return {p['source']: p['output'] for p in config['pages']}


class Links(HTMLParser):
    def __init__(self, source, config, mapping, root):
        super().__init__(convert_charrefs=False)
        self.source, self.config, self.mapping, self.root = source, config, mapping, root
        self.result = []

    def rewrite(self, value):
        url = urlsplit(value)
        if url.scheme or url.netloc or not url.path:
            return value
        path = posixpath.normpath(posixpath.join(posixpath.dirname(self.source), unquote(url.path)))
        if path.startswith('../') or path.startswith('/'):
            raise ValueError('DOCUMENT_LINK_OUTSIDE_REPOSITORY')
        target = self.root / path
        if not target.exists():
            raise ValueError('BROKEN_DOCUMENT_LINK:' + self.source + ':' + path)
        if path in self.mapping:
            result = self.config['url'] + self.mapping[path]
        else:
            kind = 'tree' if target.is_dir() else 'blob'
            result = self.config['repository'] + '/' + kind + '/' + self.config['branch'] + '/' + quote(path)
        return result + ('?' + url.query if url.query else '') + ('#' + url.fragment if url.fragment else '')

    def handle_starttag(self, tag, attrs):
        self.result.append('<' + tag + ''.join(' ' + key + ('="' + html.escape(self.rewrite(value) if key in ('href', 'src') else value, quote=True) + '"' if value is not None else '') for key, value in attrs) + '>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.result[-1] = self.result[-1][:-1] + ' />'

    def handle_endtag(self, tag):
        self.result.append('</' + tag + '>')

    def handle_data(self, data):
        self.result.append(data)

    def handle_entityref(self, name):
        self.result.append('&' + name + ';')

    def handle_charref(self, name):
        self.result.append('&#' + name + ';')

    def handle_comment(self, data):
        self.result.append('<!--' + data + '-->')


def verification_files(config, root):
    files = {}
    for name in config.get('google_verification_files', []):
        if not re.fullmatch(r'google[0-9a-f]{16}\.html', name) or name in files:
            raise ValueError('INVALID_GOOGLE_VERIFICATION_FILE')
        raw = (root / 'site/verification' / name).read_bytes()
        if raw.rstrip(b'\r\n') != ('google-site-verification: ' + name).encode('ascii'):
            raise ValueError('INVALID_GOOGLE_VERIFICATION_CONTENT')
        files[name] = raw
    return files


def build(output, root=ROOT):
    import markdown
    config = json.loads((root / 'site/config.json').read_text(encoding='utf-8'))
    mapping = pages(config)
    verification_assets = verification_files(config, root)
    # Require a new directory so no stale files or private inputs survive.
    output.mkdir(parents=True, exist_ok=False)
    (output / 'assets').mkdir()
    (output / 'assets/style.css').write_bytes((root / 'site/style.css').read_bytes())
    for name, raw in verification_assets.items():
        (output / name).write_bytes(raw)
    verification = config.get('google_site_verification', '')
    if verification and not re.fullmatch(r'[A-Za-z0-9_-]+', verification):
        raise ValueError('INVALID_GOOGLE_VERIFICATION_TAG')
    for page in config['pages']:
        md = markdown.Markdown(extensions=['tables', 'fenced_code', 'toc'], extension_configs={'toc': {'slugify': slug}})
        body = md.convert((root / page['source']).read_text(encoding='utf-8'))
        links = Links(page['source'], config, mapping, root)
        links.feed(body)
        canonical = config['url'] + page['output'] if page['output'] != 'index.html' else config['url']
        nav = '<p>DEPLOYMENT NOTES</p>' + ''.join('<a' + (' class="active" aria-current="page"' if p == page else '') + ' href="' + html.escape(config['url'] + p['output']) + '">' + html.escape(p['title']) + '</a>' for p in config['pages'])
        meta = '<meta name="google-site-verification" content="' + verification + '">' if verification else ''
        document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(page['title'])}</title><meta name="description" content="{html.escape(page['description'], quote=True)}">
<link rel="canonical" href="{canonical}"><meta property="og:type" content="website"><meta property="og:locale" content="zh_CN">
<meta property="og:title" content="{html.escape(page['title'], quote=True)}"><meta property="og:url" content="{canonical}">
<meta property="og:description" content="{html.escape(page['description'], quote=True)}">{meta}
<link rel="stylesheet" href="{posixpath.relpath('assets/style.css', posixpath.dirname(page['output']) or '.')}"><link rel="sitemap" type="application/xml" href="{config['url']}sitemap.xml"></head>
<body><a class="skip" href="#content">跳到正文</a><header><div><a class="brand" href="{config['url']}">LLMDeploy<small>ASCEND · DEPLOYMENT NOTES</small></a><a class="source" href="{config['repository']}">View on GitHub ↗</a></div></header>
<div class="layout"><nav aria-label="文档目录">{nav}<div class="nav-note">已使用经验分享<br>A5 / 950DT · A2 / A3 待验证<br>敏感信息不提供</div></nav><main id="content"><div class="eyebrow">DOCUMENTATION / 昇腾部署实践</div>{''.join(links.result)}
<div class="document-source">本文源于仓库文档 · <a href="{config['repository']}/blob/{config['branch']}/{page['source']}">查看 Markdown 源文件</a><br>仅分享已使用经验；新环境与新工件需重新验证。敏感信息不提供。</div></main></div>
<footer>LLMDeploy · 原创内容 AGPL-3.0-only，上游保留原许可 · 不提供生产、性能或兼容性保证</footer></body></html>
'''
        path = output / page['output']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding='utf-8', newline='\n')
    namespace = 'http://www.sitemaps.org/schemas/sitemap/0.9'
    ET.register_namespace('', namespace)
    sitemap = ET.Element('{' + namespace + '}urlset')
    for page in config['pages']:
        url = ET.SubElement(sitemap, '{' + namespace + '}url')
        ET.SubElement(url, '{' + namespace + '}loc').text = config['url'] + (page['output'] if page['output'] != 'index.html' else '')
    ET.ElementTree(sitemap).write(output / 'sitemap.xml', encoding='utf-8', xml_declaration=True)
    (output / 'robots.txt').write_text('User-agent: *\nAllow: /\nSitemap: ' + config['url'] + 'sitemap.xml\n', encoding='utf-8')
    (output / '.nojekyll').write_text('', encoding='utf-8')
    return len(mapping)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    print('PUBLIC_PAGES_BUILT:', build(parser.parse_args().output))
