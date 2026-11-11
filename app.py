"""Book editor: a small local web app.

	python app.py [folder-with-your-chapters]

then open http://127.0.0.1:8765 in a browser.  Documents are the .html files in the folder
(default: documents).  "Export RTF" writes a .rtf next to the .html.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

import bookdoc

HERE = Path(__file__).parent
STATIC = HERE / 'static'
VENDOR = HERE / 'vendor'
PORT = 8765
NAME_RE = re.compile(r'^[\w\- .()]+\.html$')

app = Flask(__name__, static_folder=None)
docs_dir = HERE / 'documents'


def doc_path(name: str) -> Path:
	if not NAME_RE.match(name) or name.startswith('.'):
		abort(400, 'bad file name')
	return docs_dir / name


def write_atomic(path: Path, text: str, encoding='utf-8'):
	fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
	with os.fdopen(fd, 'w', encoding=encoding, newline='\n') as f:
		f.write(text)
	os.replace(tmp, path)


@app.get('/')
def index():
	page = (STATIC / 'index.html').read_text(encoding='utf-8')
	import_map = (VENDOR / 'importmap.json').read_text(encoding='utf-8')
	return page.replace('/*IMPORTMAP*/', import_map)


@app.get('/styles.css')
def styles_css():
	# generated on every request, so editing styles.json only needs a page refresh
	return Response(bookdoc.to_css(), mimetype='text/css')


@app.get('/static/<path:name>')
def static_file(name):
	return send_from_directory(STATIC, name)


@app.get('/npm/<path:name>')
def vendor_module(name):
	return send_from_directory(VENDOR / 'npm', name, mimetype='text/javascript')


@app.get('/fonts/<path:name>')
def vendor_font(name):
	return send_from_directory(VENDOR / 'fonts', name)


@app.get('/api/files')
def list_files():
	names = sorted((p.name for p in docs_dir.glob('*.html')), key=str.lower)
	return jsonify(folder=str(docs_dir), files=names)


@app.get('/api/doc/<name>')
def read_doc(name):
	path = doc_path(name)
	if not path.exists():
		abort(404)
	return Response(path.read_text(encoding='utf-8'), mimetype='text/plain')


@app.put('/api/doc/<name>')
def save_doc(name):
	path = doc_path(name)
	if request.args.get('new') and path.exists():
		abort(409, 'file already exists')
	write_atomic(path, bookdoc.tidy_html(request.get_data(as_text=True)))
	return jsonify(saved=str(path))


@app.post('/api/export/<name>')
def export_doc(name):
	path = doc_path(name)
	if not path.exists():
		abort(404)
	target = path.with_suffix('.rtf')
	write_atomic(target, bookdoc.html_to_rtf(path.read_text(encoding='utf-8')), encoding='ascii')
	return jsonify(exported=str(target))


@app.after_request
def no_cache(response):
	response.headers['Cache-Control'] = 'no-store' if request.path.startswith(('/api', '/styles.css', '/static')) or request.path == '/' else 'max-age=86400'
	return response


if __name__ == '__main__':
	if len(sys.argv) > 1:
		docs_dir = Path(sys.argv[1]).resolve()
	docs_dir.mkdir(parents=True, exist_ok=True)
	if not (VENDOR / 'importmap.json').exists():
		sys.exit('TipTap is not downloaded yet. Run:  python fetch_vendor.py')
	print(f'Documents folder: {docs_dir}')
	print(f'Open http://127.0.0.1:{PORT}')
	# the reloader restarts the server by itself whenever one of the .py files is edited
	app.run(host='127.0.0.1', port=PORT, use_reloader=True)
