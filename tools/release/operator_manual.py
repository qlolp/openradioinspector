"""Install the offline guide from regular Git blobs belonging to the packaged commit."""
from html.parser import HTMLParser
import hashlib
import json
from pathlib import PurePosixPath
import subprocess
from urllib.parse import urlsplit


class GuideReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.local = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'img':
            self.images.append(attrs.get('src', ''))
        for key in ('src', 'href'):
            if key in attrs and not attrs[key].startswith('#'):
                self.local.append(attrs[key])


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
    for name, oid in sorted(entries.items()):
        data = subprocess.check_output(['git', 'cat-file', 'blob', oid], cwd=root)
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    guide = GuideReferences()
    guide.feed((destination / 'index.html').read_text(encoding='utf-8'))
    if not guide.images:
        raise ValueError('operator guide is missing its screenshots')
    for reference in guide.local:
        url = urlsplit(reference)
        if (url.scheme or url.netloc or url.query or not url.path or '\\' in url.path
                or '..' in PurePosixPath(url.path).parts or PurePosixPath(url.path).as_posix() not in hashes):
            raise ValueError('operator guide references content outside the bundled files')
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
