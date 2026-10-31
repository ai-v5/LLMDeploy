import importlib.util
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from scripts.build_site import ROOT, build, pages, slug, verification_files


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links, self.ids, self.meta = [], set(), {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        if 'href' in attrs:
            self.links.append(attrs['href'])
        if tag == 'meta' and 'name' in attrs:
            self.meta[attrs['name']] = attrs.get('content')


class SiteTests(unittest.TestCase):
    def test_reject_private_and_duplicate_publish_inputs(self):
        for source, output in [('local/receipt.md', 'receipt.html'), ('../README.md', 'index.html'),
                               ('docs/../local/token.md', 'index.html'), ('README.md', '../index.html')]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                pages({'pages': [{'source': source, 'output': output}]})
        config = {'pages': [{'source': 'README.md', 'output': 'index.html'}] * 2}
        with self.assertRaises(ValueError):
            pages(config)

    def test_future_platforms_are_not_advertised_as_validated(self):
        catalog = json.loads((ROOT / 'recipes/catalog.json').read_text(encoding='utf-8'))
        text = json.dumps(catalog)
        self.assertIn('planned-not-validated', text)
        self.assertIn('A2', text)
        self.assertIn('A3', text)

    def test_landing_template_is_allowed_only_as_the_homepage(self):
        self.assertEqual(pages({'pages': [{'source': 'site/home.html', 'output': 'index.html'}]}),
                         {'site/home.html': 'index.html'})
        for source, output in [('site/other.html', 'index.html'), ('site/home.html', 'other.html'),
                               ('local/home.html', 'index.html')]:
            with self.subTest(source=source, output=output), self.assertRaises(ValueError):
                pages({'pages': [{'source': source, 'output': output}]})

    @unittest.skipUnless(importlib.util.find_spec('markdown'), 'Install site/requirements.txt for Pages verification')
    def test_landing_page_shows_core_deployments_without_documentation_chrome(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'site'
            build(output)
            text = (output / 'index.html').read_text(encoding='utf-8')
            self.assertIn('class="landing"', text)
            for unwanted in ['aria-label="文档目录"', '目录与导航', '本地检查', '共用服务与运维', 'document-source']:
                self.assertNotIn(unwanted, text)
            for topology in ['单机 8 卡', '8P＋8D 分离', '16 卡 EP16 混部']:
                self.assertIn(topology, text)
            self.assertIn('双机在线 API 尚未验收', text)
            self.assertEqual(text.count('<table '), 1)
            self.assertIn('<th scope="col">DeepInfer</th>', text)
            self.assertIn('<th scope="col">vLLM Ascend</th>', text)
            self.assertIn('<th scope="row" class="model-cell">DeepSeek V4.1', text)
            self.assertIn('aria-label="文档目录"', (output / 'a5/950dt/deepinfer.html').read_text(encoding='utf-8'))

    @unittest.skipUnless(importlib.util.find_spec('markdown'), 'Install site/requirements.txt for Pages verification')
    def test_site_contains_only_allowlisted_pages_with_valid_metadata_links_and_sitemap(self):
        config = json.loads((ROOT / 'site/config.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'site'
            self.assertEqual(build(output), len(config['pages']))
            documents = {p['output']: Document((output / p['output']).read_text(encoding='utf-8')) for p in config['pages']}
            expected = set(documents) | {'assets/style.css', 'assets/home.css', 'sitemap.xml', 'robots.txt', '.nojekyll'} | set(config.get('google_verification_files', []))
            self.assertEqual({p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}, expected)
            for name, document in documents.items():
                self.assertTrue(document.meta['description'])
                self.assertNotIn('google-site-verification', document.meta)
                for link in document.links:
                    if not link.startswith(config['url']) and not link.startswith('#'):
                        continue
                    relative = link[len(config['url']):] if link.startswith(config['url']) else name + link
                    url = urlsplit(relative)
                    target = unquote(url.path) or 'index.html'
                    self.assertTrue((output / target).is_file(), (name, link))
                    if url.fragment and target in documents:
                        self.assertIn(unquote(url.fragment), documents[target].ids, (name, link))
            urls = ET.parse(output / 'sitemap.xml').findall('{*}url/{*}loc')
            self.assertEqual(len(urls), len(documents))
            self.assertEqual(len({u.text for u in urls}), len(documents))
            with self.assertRaises(FileExistsError):
                build(output)

    def test_unicode_anchors(self):
        self.assertEqual(slug('DeepInfer 共享机器让机恢复', '-'), 'deepinfer-共享机器让机恢复')

    def test_verification_cannot_publish_other_files_or_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('../README.md', '/private/key.html', 'arbitrary.html'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    verification_files({'google_verification_files': [name]}, root)
            path = root / 'site/verification/googlef1de4c0e8dff2f1b.html'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'not a Google verification marker')
            with self.assertRaisesRegex(ValueError, 'INVALID_GOOGLE_VERIFICATION_CONTENT'):
                verification_files({'google_verification_files': [path.name]}, root)

    @unittest.skipUnless(importlib.util.find_spec('markdown'), 'Install site/requirements.txt for Pages verification')
    def test_google_verification_file_is_published_byte_for_byte(self):
        config = json.loads((ROOT / 'site/config.json').read_text(encoding='utf-8'))
        self.assertEqual(config['google_verification_files'], ['googlef1de4c0e8dff2f1b.html'])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'site'
            build(output)
            for name in config['google_verification_files']:
                self.assertEqual((output / name).read_bytes(), (ROOT / 'site/verification' / name).read_bytes())
                self.assertNotIn(name, (output / 'sitemap.xml').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
