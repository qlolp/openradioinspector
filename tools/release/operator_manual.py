"""Install the offline guide from regular Git blobs belonging to the packaged commit."""
from html.parser import HTMLParser
import hashlib
import json
from pathlib import PurePosixPath
import subprocess
from urllib.parse import unquote, urlsplit


class GuideReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.local = []
        self.anchors = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.anchors.add(attrs['id'])
        if tag == 'a' and attrs.get('name'):
            self.anchors.add(attrs['name'])
        if tag == 'img':
            self.images.append(attrs.get('src') or '')
        for key in ('src', 'href'):
            if key in attrs:
                self.local.append(attrs[key] or '')


def stage_manual(root, destination):
    prefix = 'docs/operator-guide/'
    listing = subprocess.check_output(['git', 'ls-tree', '-rz', 'HEAD', '--', prefix], cwd=root)
    entries = {}
    for item in listing.rstrip(b'\0').split(b'\0') if listing else []:
        meta, name = item.decode('utf-8').split('\t', 1)
        mode, kind, oid = meta.split()
        if mode != '100644' or kind != 'blob' or not name.startswith(prefix):
            raise ValueError('operator guide must contain regular versioned resources')
        entries[name[len(prefix):]] = oid
    if 'index.html' not in entries:
        raise ValueError('offline operator guide missing from this commit')
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    pages = {}
    for name, oid in sorted(entries.items()):
        data = subprocess.check_output(['git', 'cat-file', 'blob', oid], cwd=root)
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
        if path.suffix.lower() in ('.htm', '.html'):
            page = GuideReferences()
            page.feed(data.decode('utf-8'))
            page.close()
            pages[name] = page
    shown = set()
    for name, page in pages.items():
        directory = PurePosixPath(name).parent
        for reference in page.local:
            url = urlsplit(reference)
            local = unquote(url.path)
            target = (directory / local).as_posix() if local else name
            if (url.scheme or url.netloc or url.query or (not local and not reference.startswith('#'))
                    or '\\' in local or '..' in PurePosixPath(local).parts or target not in hashes):
                raise ValueError('operator guide references content outside the bundled files')
            if url.fragment and target in pages and unquote(url.fragment) not in pages[target].anchors:
                raise ValueError(f'operator guide references a missing anchor in {target}: {reference}')
        for source in page.images:
            shown.add((directory / unquote(urlsplit(source).path)).as_posix())
    if not shown:
        raise ValueError('operator guide is missing its screenshots')
    documented = set()
    inventory = json.loads((destination / 'captures.json').read_text(encoding='utf-8'))
    for group in inventory['groups']:
        for capture in group['captures']:
            name = capture['file']
            if name in documented or name not in shown or capture['sha256'] != hashes[name]:
                raise ValueError('screenshot or checksum differs from the capture inventory: ' + name)
            documented.add(name)
    if documented != shown:
        raise ValueError('operator guide contains a screenshot without capture provenance')
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
