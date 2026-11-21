"""Check relative links in the repository README and Markdown/HTML manuals, offline."""
from functools import cache
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from tools.release.operator_manual import GuideReferences


PAGE_SUFFIXES = {'.md', '.htm', '.html'}


def read_page(path):
    if path.suffix.lower() == '.md':
        rendered = subprocess.check_output(
            ['pandoc', '--from=gfm', '--to=html5', '--no-highlight', str(path)],
            encoding='utf-8',
        )
    else:
        rendered = path.read_text(encoding='utf-8')
    page = GuideReferences()
    page.feed(rendered)
    page.close()
    return page


def check(root):
    root = Path(root).resolve()
    load_page = cache(read_page)
    manuals = sorted(p for p in (root / 'docs').rglob('*') if p.suffix.lower() in PAGE_SUFFIXES)
    pages = [root / 'README.md', *manuals]
    links = 0
    anchors = 0
    problems = []
    for source in pages:
        for reference in load_page(source).local:
            url = urlsplit(reference)
            if url.scheme or url.netloc:
                continue
            links += 1
            target = (source.parent / unquote(url.path)).resolve() if url.path else source
            if not target.is_relative_to(root) or not target.exists():
                problems.append(f'{source.relative_to(root)} -> {reference} (missing local path)')
                continue
            if url.fragment:
                if target.is_dir():
                    target = target / 'README.md'
                if target.suffix.lower() in PAGE_SUFFIXES:
                    anchors += 1
                    if not target.is_file() or unquote(url.fragment) not in load_page(target).anchors:
                        problems.append(f'{source.relative_to(root)} -> {reference} (missing anchor)')
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f'{len(pages)} pages, {links} local links, {anchors} document anchors, {len(problems)} broken')
    return int(bool(problems))


if __name__ == '__main__':
    try:
        sys.exit(check(Path(__file__).resolve().parents[2]))
    except FileNotFoundError as error:
        if error.filename != 'pandoc':
            raise
        sys.exit('The documentation link check requires pandoc with the gfm reader.')
