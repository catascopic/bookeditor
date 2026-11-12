"""Book editor: a small local web app.

	python app.py [folder-with-your-chapters] [--port N]

then open http://127.0.0.1:8765 in a browser.  The server restarts itself when a .py file
changes, and on Ctrl+C (press it twice to quit).  Documents are the .html files in the folder
(default: documents).  "Export RTF" writes a .rtf next to the .html.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from werkzeug.serving import make_server

import bookdoc

HERE = Path(__file__).parent
STATIC = HERE / 'static'
VENDOR = HERE / 'vendor'
HOST = '127.0.0.1'
PORT = 8765
IMAGE_EXTENSIONS = {'png': '.png', 'jpeg': '.jpg'}
RESTART_DELAY = 1  # seconds between Ctrl+C and the restart
WATCH_INTERVAL = 0.5  # seconds between checks for changed code
WATCH_SKIP = {'vendor', 'documents', 'static', '__pycache__'}
CHILD_ENV = 'BOOKEDITOR_SERVER'  # set in the process that actually serves
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


@app.get('/api/senders')
def read_senders():
	return jsonify(senders=bookdoc.load_senders(docs_dir))


@app.put('/api/senders')
def save_senders():
	senders = bookdoc.clean_senders(request.get_json(force=True))
	write_atomic(docs_dir / bookdoc.SENDERS_FILE, json.dumps(senders, indent='\t', ensure_ascii=False) + '\n')
	return jsonify(senders=senders)


@app.get('/images/<path:name>')
def read_image(name):
	# documents refer to their pictures as "images/<file>", relative to the documents folder
	return send_from_directory(docs_dir / 'images', name)


@app.post('/api/image')
def save_image():
	"""Store an uploaded picture in <documents>/images and say what to call it."""
	data = request.get_data()
	info = bookdoc.image_info(data)
	if not info:
		abort(415, 'only PNG and JPEG pictures can be used')
	stem = re.sub(r'[^\w\- ()]+', '-', Path(request.args.get('name') or 'picture').stem).strip('- ') or 'picture'
	folder = docs_dir / 'images'
	folder.mkdir(exist_ok=True)
	number = 1
	while True:
		path = folder / f"{stem}{'' if number == 1 else f'-{number}'}{IMAGE_EXTENSIONS[info[0]]}"
		if not path.exists():
			path.write_bytes(data)
			break
		if path.read_bytes() == data:
			break  # the same picture again: reuse it
		number += 1
	return jsonify(src=f'images/{path.name}')


@app.post('/api/export/<name>')
def export_doc(name):
	path = doc_path(name)
	if not path.exists():
		abort(404)
	target = path.with_suffix('.rtf')
	write_atomic(target, bookdoc.html_to_rtf(path.read_text(encoding='utf-8'), base_dir=path.parent), encoding='ascii')
	return jsonify(exported=str(target))


@app.after_request
def no_cache(response):
	response.headers['Cache-Control'] = 'no-store' if request.path.startswith(('/api', '/styles.css', '/static', '/images')) or request.path == '/' else 'max-age=86400'
	return response


def source_snapshot():
	"""Modification times of every .py file in the project, to notice code changes.

	Watching the whole folder is simpler than tracking which modules are imported, and it
	also covers a file that failed to import. Pages, scripts and styles.json are read fresh
	on each request, so they need no restart -- only a browser refresh.
	"""
	snapshot = {}
	for folder, subfolders, files in os.walk(HERE):
		subfolders[:] = [d for d in subfolders if d not in WATCH_SKIP and not d.startswith('.')]
		for name in files:
			if name.endswith('.py'):
				path = os.path.join(folder, name)
				try:
					snapshot[path] = os.stat(path).st_mtime_ns
				except OSError:
					pass  # deleted or mid-save
	return snapshot


def require_free_port(port):
	with socket.socket() as probe:
		try:
			probe.bind((HOST, port))
		except OSError:
			raise SystemExit(
				f'Port {port} is already in use. Is the editor already running? '
				f'Stop that copy, or pick another port with --port.') from None


def supervise(port):
	"""Run the server in a child process, and start a new one when asked to.

	- A .py file changes: restart straight away.
	- Ctrl+C: restart after RESTART_DELAY seconds; a second Ctrl+C in that time exits.
	- The server dies by itself (say, a syntax error in an edit): wait for the next code
	  change and try again.

	The supervisor itself only ever runs this function, so it survives any broken edit.
	"""
	require_free_port(port)
	while True:
		before = source_snapshot()
		child = subprocess.Popen([sys.executable, *sys.argv], env={**os.environ, CHILD_ENV: '1'})
		try:
			while child.poll() is None and source_snapshot() == before:
				time.sleep(WATCH_INTERVAL)
			if child.poll() is None:
				print('\n--- code changed, restarting ---', flush=True)
				child.terminate()
				child.wait()
				continue
			print(f'\n--- server stopped (exit code {child.returncode}); '
				'waiting for a code change to try again, Ctrl+C to restart now ---', flush=True)
			while source_snapshot() == before:
				time.sleep(WATCH_INTERVAL)
		except KeyboardInterrupt:
			try:
				# the child shares this console, so it received the same Ctrl+C
				try:
					child.wait(timeout=5)
				except subprocess.TimeoutExpired:
					child.terminate()
					child.wait()
				print(f'\nRestarting in {RESTART_DELAY:g}s (press Ctrl+C again to exit)', flush=True)
				time.sleep(RESTART_DELAY)
			except KeyboardInterrupt:
				if child.poll() is None:
					child.terminate()
				return


def serve(port):
	if not (VENDOR / 'importmap.json').exists():
		sys.exit('TipTap is not downloaded yet. Run:  python fetch_vendor.py')
	docs_dir.mkdir(parents=True, exist_ok=True)
	print(f'Documents folder: {docs_dir}')
	print(f'Open http://{HOST}:{port}', flush=True)
	try:
		make_server(HOST, port, app, threaded=True).serve_forever()
	except KeyboardInterrupt:
		pass


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Book editor server')
	parser.add_argument('folder', nargs='?', help='folder holding your chapters (default: documents)')
	parser.add_argument('-p', '--port', type=int, default=PORT, help=f'server port number (default: {PORT})')
	args = parser.parse_args()
	if os.environ.get(CHILD_ENV):
		if args.folder:
			docs_dir = Path(args.folder).resolve()
		serve(args.port)
	else:
		supervise(args.port)
