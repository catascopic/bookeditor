"""Download TipTap (and everything it depends on) plus the book fonts into vendor
so the editor works offline.

Run once:  python fetch_vendor.py

It fetches browser-ready ES modules from the jsDelivr CDN.  Those modules import each
other by absolute path ("/npm/<package>@<version>/+esm"), so we store them under
vendor/npm/... and the Flask app serves that folder at /npm/.  No Node.js needed.
"""
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

CDN = 'https://cdn.jsdelivr.net'
VENDOR = Path(__file__).parent / 'vendor'

# Bare import name used in editor.js -> (npm package, version range, sub-path)
ROOTS = {
	'@tiptap/core': ('@tiptap/core', '2', ''),
	'@tiptap/starter-kit': ('@tiptap/starter-kit', '2', ''),
	'@tiptap/pm/state': ('@tiptap/pm', '2', '/state'),
	'@tiptap/pm/view': ('@tiptap/pm', '2', '/view'),
}

# The book's fonts, as web fonts, so the editor shows them even where they aren't installed.
# (family name, fontsource package, file stem, [(weight, style, file suffix)])
FONTS = [
	('Source Serif 4', '@fontsource-variable/source-serif-4@5', 'source-serif-4',
		[('200 900', 'normal', 'wght-normal'), ('200 900', 'italic', 'wght-italic')]),
	('Source Sans 3', '@fontsource-variable/source-sans-3@5', 'source-sans-3',
		[('200 900', 'normal', 'wght-normal'), ('200 900', 'italic', 'wght-italic')]),
	('IBM Plex Mono', '@fontsource/ibm-plex-mono@5', 'ibm-plex-mono',
		[('400', 'normal', '400-normal'), ('400', 'italic', '400-italic'),
		 ('700', 'normal', '700-normal'), ('700', 'italic', '700-italic')]),
]
SUBSETS = {
	'latin': 'U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,'
		'U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD',
	'latin-ext': 'U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+1D00-1DBF,U+1E00-1E9F,'
		'U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF',
}

IMPORT_RE = re.compile(r'''(?:from|import)\s*["'](/npm/[^"']+)["']''')
PKG_RE = re.compile(r'^/npm/((?:@[^/]+/)?[^/@]+)@([^/]+)(/.*)?$')


def fetch(path: str):
	req = urllib.request.Request(CDN + path, headers={'User-Agent': 'vendor-fetch'})
	with urllib.request.urlopen(req, timeout=60) as resp:
		return resp.read(), resp.headers


def version_key(version: str):
	return tuple(int(n) for n in re.findall(r'\d+', version))


def pin(path: str, pins: dict):
	pkg, _, rest = PKG_RE.match(path).groups()
	return f'/npm/{pkg}@{pins[pkg]}{rest or ""}' if pkg in pins else path


def crawl(roots: list, pins: dict):
	"""Download roots and everything they import. Returns (paths, {package: {versions}})."""
	if (VENDOR / 'npm').exists():
		shutil.rmtree(VENDOR / 'npm')
	seen, versions = set(), {}
	queue = [pin(r, pins) for r in roots]
	while queue:
		path = queue.pop()
		if path in seen:
			continue
		seen.add(path)
		text = fetch(path)[0].decode('utf-8')
		for dep in set(IMPORT_RE.findall(text)):
			pkg, version, _ = PKG_RE.match(dep).groups()
			versions.setdefault(pkg, set()).add(version)
			text = text.replace(dep, pin(dep, pins))
			queue.append(pin(dep, pins))
		target = VENDOR / path.lstrip('/')
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(text, encoding='utf-8', newline='')
		print(f'{len(text):>8}  {path}')
	for path in roots:
		pkg, version, _ = PKG_RE.match(path).groups()
		versions.setdefault(pkg, set()).add(version)
	return seen, versions


def fetch_fonts():
	out = VENDOR / 'fonts'
	out.mkdir(parents=True, exist_ok=True)
	css = []
	for family, pkg, stem, variants in FONTS:
		for weight, style, suffix in variants:
			for subset, unicode_range in SUBSETS.items():
				name = f'{stem}-{subset}-{suffix}.woff2'
				body, _ = fetch(f'/npm/{pkg}/files/{name}')
				(out / name).write_bytes(body)
				print(f'{len(body):>8}  {name}')
				css.append(
					f'@font-face {{ font-family: "{family}"; font-style: {style}; font-weight: {weight}; '
					f'src: local("{family}"), url("/fonts/{name}") format("woff2"); unicode-range: {unicode_range}; }}')
	(out / 'fonts.css').write_text('\n'.join(css) + '\n', encoding='utf-8')


def main():
	if '--fonts-only' in sys.argv:
		return fetch_fonts()
	import_map = {}
	queue = []
	for name, (pkg, version_range, sub) in ROOTS.items():
		# Resolve the range ("2") to an exact version so every module agrees on one URL.
		_, headers = fetch(f'/npm/{pkg}@{version_range}/package.json')
		version = headers.get('x-jsd-version')
		if not version:
			sys.exit(f'could not resolve version of {pkg}')
		path = f'/npm/{pkg}@{version}{sub}/+esm'
		import_map[name] = path
		queue.append(path)

	# Different packages can ask for slightly different versions of the same dependency.
	# Two copies of ProseMirror in one page breaks it, so pin every package to the newest
	# version anyone asked for, rewrite the imports to match, and repeat until it settles.
	pins = {}
	while True:
		seen, versions = crawl(list(import_map.values()), pins)
		newest = {pkg: max(v | ({pins[pkg]} if pkg in pins else set()), key=version_key) for pkg, v in versions.items()}
		if newest == pins:
			break
		pins = newest
		print('-- pinning versions and fetching again --')

	import_map = {name: pin(path, pins) for name, path in import_map.items()}
	(VENDOR / 'importmap.json').write_text(json.dumps({'imports': import_map}, indent='\t'), encoding='utf-8')
	print(f'\n{len(seen)} modules, {len(versions)} packages')


if __name__ == '__main__':
	main()
