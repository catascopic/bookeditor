"""The book document format: read the saved HTML, and turn it into RTF or editor CSS.

A document on disk is an ordinary HTML file: a short fixed <head>, then a <body> with one
block per line, using only these shapes:

	<h1>…</h1>                      chapter header
	<h2>…</h2>                      section header
	<p>…</p>                        body paragraph
	<hr>                            dinkus
	<pre><code>…</code></pre>       code block
	<figure><img src="images/x.png"><figcaption>…</figcaption></figure>   picture with caption
	<div class="journal"><p>…</p>…</div>
	<div class="email"><div class="email-from">…</div><div class="email-to">…</div><p>…</p>…</div>

Inline: <strong>, <em>, <code> (inline code) and <br>.

The <head> links to book.css, which the editor writes into the documents folder, so a
chapter opened straight from disk in a browser looks the way it does in the editor.
Everything in the <head>, and the data-from attribute on emails, is rewritten on every
save: only the blocks in the <body> are the document.

Can also be run directly:  python bookdoc.py chapter.html  ->  writes chapter.rtf
"""
import colorsys
import html as htmllib
import json
import re
import struct
import sys
from html.parser import HTMLParser
from pathlib import Path

STYLES_PATH = Path(__file__).parent / 'styles.json'

# A run is (text, marks) where marks is a frozenset drawn from {'b', 'i', 'code'}.
MARK_TAGS = {'strong': 'b', 'b': 'b', 'em': 'i', 'i': 'i', 'code': 'code'}
TEXT_TAGS = {'h1': 'chapter', 'h2': 'section', 'p': 'para', 'pre': 'code'}


def load_styles():
	return json.loads(STYLES_PATH.read_text(encoding='utf-8'))


# ---------------------------------------------------------------- email senders
#
# Each folder of documents has a senders.json: [{"name": "ALICE", "color": "#b3261e"}, ...].
# An email whose From line is that name (capitals and spacing aside) is drawn in that colour.

SENDERS_FILE = 'senders.json'
COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def clean_senders(senders) -> list[dict]:
	"""Keep only well-formed entries, so nothing odd reaches the page's CSS or the RTF."""
	cleaned = []
	for sender in senders if isinstance(senders, list) else []:
		if not isinstance(sender, dict):
			continue
		name, color = str(sender.get('name') or '').strip(), str(sender.get('color') or '')
		if name and COLOR_RE.match(color):
			cleaned.append({'name': name, 'color': color.lower()})
	return cleaned


def load_senders(base_dir) -> list[dict]:
	try:
		return clean_senders(json.loads((Path(base_dir) / SENDERS_FILE).read_text(encoding='utf-8')))
	except (OSError, ValueError, TypeError):
		return []


def sender_key(name: str) -> str:
	return ' '.join(name.split()).casefold()


def text_color_on(color: str) -> str:
	"""White or black, whichever reads better on the given background colour."""
	r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
	return '#000000' if colorsys.rgb_to_yiq(r / 255, g / 255, b / 255)[0] > 0.6 else '#ffffff'


# ---------------------------------------------------------------- HTML -> blocks

class _DocParser(HTMLParser):
	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.blocks = []
		self.container = None  # the email/journal block being filled, if any
		self.figure = None  # the picture block being filled, if any
		self.runs = None  # runs of the text block being read, if any
		self.kind = None
		self.marks = []

	def handle_starttag(self, tag, attrs):
		classes = (dict(attrs).get('class') or '').split()
		if tag in TEXT_TAGS:
			self._open(TEXT_TAGS[tag])
		elif tag == 'div' and 'email-from' in classes:
			self._open('from')
		elif tag == 'div' and 'email-to' in classes:
			self._open('to')
		elif tag == 'div' and 'email' in classes:
			self.container = {'type': 'email', 'from': [], 'to': [], 'paras': []}
		elif tag == 'div' and 'journal' in classes:
			self.container = {'type': 'journal', 'paras': []}
		elif tag == 'figure':
			self.figure = {'type': 'figure', 'src': None, 'runs': []}
		elif tag == 'img' and self.figure is not None:
			self.figure['src'] = dict(attrs).get('src')
		elif tag == 'figcaption':
			self._open('caption')
		elif tag == 'hr':
			self.blocks.append({'type': 'dinkus'})
		elif tag == 'br':
			self.handle_data('\n')
		elif tag in MARK_TAGS and self.kind != 'code':
			self.marks.append(MARK_TAGS[tag])

	def handle_endtag(self, tag):
		if tag in TEXT_TAGS or tag == 'figcaption' or (tag == 'div' and self.runs is not None):
			self._close()
		elif tag == 'figure' and self.figure is not None:
			self.blocks.append(self.figure)
			self.figure = None
		elif tag == 'div' and self.container is not None:
			self.blocks.append(self.container)
			self.container = None
		elif tag in MARK_TAGS and self.kind != 'code' and MARK_TAGS[tag] in self.marks:
			self.marks.remove(MARK_TAGS[tag])

	def handle_data(self, data):
		if self.runs is None:
			return  # whitespace between blocks
		marks = frozenset(self.marks)
		if self.runs and self.runs[-1][1] == marks:
			self.runs[-1] = (self.runs[-1][0] + data, marks)
		else:
			self.runs.append((data, marks))

	def _open(self, kind):
		self.kind, self.runs, self.marks = kind, [], []

	def _close(self):
		if self.runs is None:
			return
		kind, runs = self.kind, self.runs
		self.kind = self.runs = None
		if kind == 'caption':
			if self.figure is not None:
				self.figure['runs'] = runs
		elif kind in ('from', 'to'):
			if self.container and self.container['type'] == 'email':
				self.container[kind] = runs
		elif kind == 'para' and self.container is not None:
			self.container['paras'].append(runs)
		else:
			self.blocks.append({'type': 'body' if kind == 'para' else kind, 'runs': runs})


def parse_html(html: str) -> list[dict]:
	parser = _DocParser()
	parser.feed(html)
	parser.close()
	return parser.blocks


_BLOCK_BREAK = re.compile(r'(</p>|</h1>|</h2>|</pre>|</div>|</figure>|<hr[^>]*>|<div class="(?:email|journal)">)(?!\n)')


def tidy_html(html: str) -> str:
	"""Put each block on its own line so the saved file reads and diffs well."""
	return _BLOCK_BREAK.sub(r'\1\n', html.strip()) if html.strip() else '<p></p>\n'


# ---------------------------------------------------------------- the file on disk

DOC_FORMAT = 1
BOOK_CSS = 'book.css'
_BODY = re.compile(r'<body[^>]*>(.*)</body>', re.DOTALL | re.IGNORECASE)
_EMAIL_FROM = re.compile(r'<div class="email">(\s*)<div class="email-from">(.*?)</div>', re.DOTALL)
_TAG = re.compile(r'<[^>]+>')


def body_of(document: str) -> str:
	"""The blocks of a saved file. Files from before the wrapper existed are all blocks."""
	match = _BODY.search(document)
	return (match.group(1) if match else document).strip() + '\n'


def _tag_sender(match):
	# lets book.css colour the email by sender; the editor works the colour out for itself
	key = sender_key(htmllib.unescape(_TAG.sub('', match.group(2))))
	return (
		f'<div class="email" data-from="{htmllib.escape(key, quote=True)}">{match.group(1)}'
		f'<div class="email-from">{match.group(2)}</div>')


def wrap_document(blocks_html: str, fallback_title: str) -> str:
	"""A complete HTML file around the editor's blocks. Titled after the chapter header, if any."""
	body = tidy_html(blocks_html)
	title = next(
		(' '.join(''.join(text for text, _ in block['runs']).split())
			for block in parse_html(body) if block['type'] == 'chapter'), '') or fallback_title
	return (
		'<!DOCTYPE html>\n'
		'<html lang="en">\n'
		'<head>\n'
		'<meta charset="utf-8">\n'
		f'<meta name="generator" content="bookeditor {DOC_FORMAT}">\n'
		f'<title>{htmllib.escape(title)}</title>\n'
		f'<link rel="stylesheet" href="{BOOK_CSS}">\n'
		'</head>\n'
		'<body>\n'
		f'{_EMAIL_FROM.sub(_tag_sender, body)}'
		'</body>\n'
		'</html>\n')


# ---------------------------------------------------------------- blocks -> RTF

def _twips_in(inches):
	return round((inches or 0) * 1440)


def _twips_pt(points):
	return round((points or 0) * 20)


def _escape(text: str) -> str:
	out = []
	for ch in text:
		if ch in '\\{}':
			out.append('\\' + ch)
		elif ch == '\n':
			out.append('\\line ')
		elif ch == '\t':
			out.append('\\tab ')
		elif ord(ch) < 128:
			out.append(ch)
		else:
			# \uN takes a signed 16-bit number; characters beyond that go out as a surrogate pair.
			data = ch.encode('utf-16-le')
			for i in range(0, len(data), 2):
				out.append(f"\\u{int.from_bytes(data[i:i + 2], 'little', signed=True)}?")
	return ''.join(out)


IMAGE_SRC_RE = re.compile(r'^images/[\w\- .()]+\.(png|jpe?g)$', re.IGNORECASE)
SCREEN_DPI = 96  # a picture's natural size on screen: 96 pixels to the inch


def image_info(data: bytes):
	"""(kind, width, height) in pixels for a PNG or JPEG, or None for anything else."""
	if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 24:
		return ('png', *struct.unpack('>II', data[16:24]))
	if data[:2] == b'\xff\xd8':
		i = 2
		while i + 9 < len(data) and data[i] == 0xFF:
			marker = data[i + 1]
			if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # a start-of-frame segment
				height, width = struct.unpack('>HH', data[i + 5:i + 9])
				return ('jpeg', width, height)
			i += 2 + struct.unpack('>H', data[i + 2:i + 4])[0]
	return None


class _RtfBuilder:
	def __init__(self, styles, base_dir=None):
		self.base_dir = Path(base_dir) if base_dir else None
		self.styles = styles
		self.blocks = styles['blocks']
		self.fonts = []
		self.colors = []
		self.senders = {sender_key(s['name']): s['color'] for s in load_senders(base_dir)} if base_dir else {}
		self.out = []

	def color(self, color):
		"""Index of a #rrggbb colour in the RTF colour table (which counts from 1; 0 is 'automatic')."""
		if color not in self.colors:
			self.colors.append(color)
		return self.colors.index(color) + 1

	def font(self, name):
		if name not in self.fonts:
			self.fonts.append(name)
		return self.fonts.index(name)

	def runs(self, runs, style):
		parts = []
		for text, marks in runs:
			codes = ''
			inline_code = self.styles['inline_code']
			if 'b' in marks or ('code' in marks and inline_code.get('bold')):
				codes += '\\b'
			if 'i' in marks:
				# emphasis inside already-italic text goes upright
				codes += '\\i0' if style.get('italic') else '\\i'
			if 'code' in marks:
				codes += f"\\f{self.font(inline_code['font'])}"
			parts.append(f'{{{codes} {_escape(text)}}}' if codes else _escape(text))
		return ''.join(parts)

	def para(self, style, content, before=0, after=0, font=None, bold=None, para_codes='', char_codes=''):
		bold = style.get('bold') if bold is None else bold
		codes = ['\\pard']
		if style.get('keep_with_next'):
			codes.append('\\keepn')
		codes.append('\\nowidctlpar')
		for word, value in (
			('fi', _twips_in(style.get('first_line_in'))),
			('li', _twips_in(style.get('indent_in'))),
			('ri', _twips_in(style.get('right_indent_in'))),
			('sb', _twips_pt(before)),
			('sa', _twips_pt(after)),
		):
			if value:
				codes.append(f'\\{word}{value}')
		codes.append(para_codes)
		codes.append('\\sl240\\slmult1')
		if style.get('align') == 'center':
			codes.append('\\qc')
		codes.append(f"\\plain\\f{self.font(font or style['font'])}\\fs{round(style['size'] * 2)}")
		if bold:
			codes.append('\\b')
		if style.get('italic'):
			codes.append('\\i')
		codes.append(char_codes)
		self.out.append(f"{''.join(codes)} {content}\\par\n")

	def container(self, style, paras, first_before, **extra):
		"""Paragraphs of an email or journal: spaced apart, with the block's own space at the ends."""
		for i, runs in enumerate(paras):
			last = i == len(paras) - 1
			self.para(
				style, self.runs(runs, style),
				before=first_before if i == 0 else 0,
				after=style.get('space_after') if last else style.get('para_spacing'), **extra)

	def email(self, block, style):
		"""A box in the sender's colour, with From/To on a band of that colour across the top.

		RTF has no boxes as such: each paragraph gets the same border on all four sides, and
		word processors that honour paragraph borders (Word does) join neighbours into one box.
		"""
		sender = sender_key(''.join(text for text, _ in block['from']))
		color = self.senders.get(sender) or style.get('default_color') or '#000000'
		ink, paper = self.color(color), self.color(text_color_on(color))
		width, gap = _twips_pt(style.get('border_width', 1)), _twips_pt(style.get('box_padding'))
		box = ''.join(f'\\brdr{side}\\brdrs\\brdrw{width}\\brsp{gap}\\brdrcf{ink}' for side in 'tlbr')
		band = dict(
			font=style.get('header_font'), bold=style.get('header_bold'),
			para_codes=f'{box}\\cbpat{ink}', char_codes=f'\\cf{paper}')
		self.para(style, _escape(style['from_label']) + self.runs(block['from'], style), before=style.get('space_before'), **band)
		self.para(style, _escape(style['to_label']) + self.runs(block['to'], style), **band)
		self.container(style, block['paras'], first_before=0, para_codes=box)

	def picture(self, src, style):
		"""The RTF for an embedded picture, scaled down if it is wider than the style allows."""
		data = None
		if self.base_dir and src and IMAGE_SRC_RE.match(src):
			try:
				data = (self.base_dir / src).read_bytes()
			except OSError:
				pass
		info = data and image_info(data)
		if not info:
			return _escape(f'[missing picture: {src}]')
		kind, width, height = info
		width_in = min(width / SCREEN_DPI, style.get('max_width_in') or width / SCREEN_DPI)
		height_in = width_in * height / width
		digits = data.hex()
		lines = '\n'.join(digits[i:i + 128] for i in range(0, len(digits), 128))
		return (
			f'{{\\pict\\{kind}blip\\picw{width}\\pich{height}'
			f'\\picwgoal{_twips_in(width_in)}\\pichgoal{_twips_in(height_in)}\n{lines}}}')

	def figure(self, block):
		style, body = self.blocks['figure'], self.blocks['body']
		# centred, in the body's font, held in from both margins like the other set-off blocks
		text = {
			'font': body['font'], 'size': body['size'], 'bold': body.get('bold'), 'italic': body.get('italic'),
			'align': 'center', 'indent_in': style.get('indent_in'), 'right_indent_in': style.get('indent_in')}
		captioned = any(t.strip() for t, _ in block['runs'])
		self.para(
			{**text, 'keep_with_next': captioned}, self.picture(block['src'], style),
			before=style.get('space_before'), after=style.get('caption_gap') if captioned else style.get('space_after'))
		if captioned:
			self.para(text, self.runs(block['runs'], text), after=style.get('space_after'))

	def block(self, block):
		kind = block['type']
		if kind == 'figure':
			return self.figure(block)
		style = self.blocks[kind]
		before, after = style.get('space_before'), style.get('space_after')
		if kind == 'dinkus':
			symbol = self.font(style.get('symbol_font') or style['font'])
			content = ''.join(c if c == ' ' else f'{{\\f{symbol} {_escape(c)}}}' for c in style['text'])
			self.para(style, content, before, after)
		elif kind == 'email':
			self.email(block, style)
		elif kind == 'journal':
			self.container(style, block['paras'], first_before=before)
		else:
			self.para(style, self.runs(block['runs'], style), before, after)

	def document(self, blocks):
		for block in blocks:
			self.block(block)
		fonts = ''.join(f'{{\\f{i}\\fnil\\fcharset0 {name};}}' for i, name in enumerate(self.fonts))
		colors = ''.join(
			f'\\red{int(c[1:3], 16)}\\green{int(c[3:5], 16)}\\blue{int(c[5:7], 16)};' for c in self.colors)
		return (
			'{\\rtf1\\ansi\\ansicpg1252\\deff0\\deflang1033'
			f'{{\\fonttbl{fonts}}}\n'
			+ (f'{{\\colortbl ;{colors}}}\n' if colors else '') +
			'{\\*\\generator bookeditor;}\\viewkind4\\uc1\n'
			+ ''.join(self.out) + '}\n')


def to_rtf(blocks: list[dict], styles=None, base_dir=None) -> str:
	"""base_dir is the folder the document lives in; pictures are found relative to it."""
	return _RtfBuilder(styles or load_styles(), base_dir).document(blocks)


def html_to_rtf(html: str, styles=None, base_dir=None) -> str:
	return to_rtf(parse_html(html), styles, base_dir)


# ---------------------------------------------------------------- styles -> CSS

def _generic(font):
	lowered = font.lower()
	if 'mono' in lowered or 'courier' in lowered:
		return 'monospace'
	return 'sans-serif' if 'sans' in lowered else 'serif'


def _family(font):
	return f'"{font}", {_generic(font)}'


def _css_text(style, font=None, bold=None):
	bold = style.get('bold') if bold is None else bold
	return (
		f"font-family: {_family(font or style['font'])}; font-size: {style['size']}pt; "
		f"font-weight: {'bold' if bold else 'normal'}; font-style: {'italic' if style.get('italic') else 'normal'}; "
		f"text-align: {style.get('align', 'left')}; ")


def _css_box(style):
	# padding rather than margin, so neighbouring blocks' spacing adds up the way it does in RTF
	return (
		f"padding: {style.get('space_before', 0)}pt 0 {style.get('space_after', 0)}pt {style.get('indent_in', 0)}in; "
		f"text-indent: {style.get('first_line_in', 0)}in; ")


def _css_string(text):
	return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def to_css(styles=None, root='.ProseMirror', chrome=True) -> str:
	"""The look of every block type, as CSS.

	root is the element holding the blocks: the editor's text area, or <body> for book.css.
	chrome adds the rules for the editor's own page and toolbar.
	"""
	styles = styles or load_styles()
	b = styles['blocks']
	rules = [f".page {{ width: {styles['editor']['column_width_in']}in; }}"] if chrome else []
	for selector, kind in (('h1', 'chapter'), ('h2', 'section'), ('> p', 'body'), ('pre', 'code'), ('hr', 'dinkus')):
		rules.append(f'{root} {selector} {{ {_css_text(b[kind])}{_css_box(b[kind])}}}')
	rules.append(f"{root} hr::before {{ content: {_css_string(b['dinkus']['text'])}; }}")
	rules.append(f"{root} h2 em {{ font-style: {'normal' if b['section'].get('italic') else 'italic'}; }}")
	inline_code = styles['inline_code']
	inline_code_css = f"font-family: {_family(inline_code['font'])}; font-weight: {'bold' if inline_code.get('bold') else 'inherit'};"
	rules.append(f"{root} :not(pre) > code {{ {inline_code_css} }}")
	s = b['journal']
	rules.append(f'{root} div.journal {{ {_css_text(s)}{_css_box(s)}}}')
	rules.append(f"{root} div.journal > p {{ margin-bottom: {s.get('para_spacing', 0)}pt; }}")
	rules.append(f'{root} div.journal > p:last-child {{ margin-bottom: 0; }}')
	# An email is a box in the sender's colour. The editor sets --sender and --sender-text on
	# each email from its From line; without them the default colour applies.
	e = b['email']
	pad = f"{e.get('box_padding', 0)}pt"
	default = e.get('default_color', '#000000')
	sender, sender_text = f'var(--sender, {default})', f'var(--sender-text, {text_color_on(default)})'
	rules.append(
		f"{root} div.email {{ {_css_text(e)}text-indent: 0; "
		f"margin: {e.get('space_before', 0)}pt {e.get('right_indent_in', 0)}in {e.get('space_after', 0)}pt {e.get('indent_in', 0)}in; "
		f"border: {e.get('border_width', 1)}pt solid {sender}; padding-bottom: {pad}; }}")
	rules.append(f"{root} div.email > p {{ margin: 0 {pad} {e.get('para_spacing', 0)}pt; }}")
	rules.append(f'{root} div.email > .email-to + p {{ margin-top: {pad}; }}')
	rules.append(f'{root} div.email > p:last-child {{ margin-bottom: 0; }}')
	rules.append(
		f"{root} .email-from, {root} .email-to {{ {_css_text(e, e.get('header_font'), e.get('header_bold'))}"
		f"background: {sender}; color: {sender_text}; padding: 0 {pad}; }}")
	rules.append(f'{root} .email-from {{ padding-top: 3pt; }}')
	rules.append(f'{root} .email-to {{ padding-bottom: 3pt; }}')
	rules.append(f"{root} .email-from::before {{ content: {_css_string(e['from_label'])}; }}")
	rules.append(f"{root} .email-to::before {{ content: {_css_string(e['to_label'])}; }}")
	f, body = b['figure'], b['body']
	rules.append(
		f"{root} figure {{ {_css_text({**body, 'align': 'center'})}"
		f"padding: {f.get('space_before', 0)}pt {f.get('indent_in', 0)}in {f.get('space_after', 0)}pt; text-indent: 0; }}")
	rules.append(f"{root} figure img {{ max-width: min(100%, {f.get('max_width_in', 100)}in); }}")
	rules.append(f"{root} figcaption {{ padding-top: {f.get('caption_gap', 0)}pt; }}")
	if not chrome:
		return '\n'.join(rules) + '\n'
	# the editor's rule down the side of a journal or code block covers the text, not its spacing
	for selector, kind in (('div.journal', 'journal'), ('pre', 'code')):
		rules.append(
			f"{root} {selector}::after {{ top: {b[kind].get('space_before', 0)}pt; "
			f"bottom: {b[kind].get('space_after', 0)}pt; }}")
	# toolbar buttons are drawn in the font of the thing they produce
	for kind in ('body', 'chapter', 'section', 'email', 'journal', 'code'):
		s = b[kind]
		rules.append(
			f".toolbar [data-block=\"{kind}\"] {{ font-family: {_family(s['font'])}; "
			f"font-weight: {'bold' if s.get('bold') else 'normal'}; font-style: {'italic' if s.get('italic') else 'normal'}; }}")
	rules.append(f".toolbar [data-mark=\"bold\"], .toolbar [data-mark=\"italic\"] {{ font-family: {_family(b['body']['font'])}; }}")
	rules.append(f".toolbar [data-mark=\"code\"] {{ {inline_code_css} }}")
	return '\n'.join(rules) + '\n'


# What the editor's own stylesheet does for its text area, restated for a bare <body>.
_BOOK_CSS_BASE = """\
/* Written by the book editor from styles.json and senders.json. Edits here are overwritten. */
html { background: #e9e7e2; }
body {
	box-sizing: content-box; width: %(width)sin; margin: 24px auto; padding: 0.9in 1in;
	background: #fff; color: #000; line-height: 1.25; font-variant-ligatures: none;
	box-shadow: 0 1px 4px rgb(0 0 0 / 18%%);
}
body p, body h1, body h2, body pre, body hr, body figure { margin: 0; }
body p, body h1, body h2, body pre, body figcaption, body .email-from, body .email-to { white-space: pre-wrap; overflow-wrap: break-word; }
body hr { border: 0; height: auto; }
body pre code { font: inherit; }
body :not(pre) > code { font-size: inherit; }
body figure img { display: block; margin: 0 auto; height: auto; }
body figcaption:empty { display: none; }
body div.journal > p { text-indent: 0; }
@media print {
	html { background: none; }
	body { width: auto; margin: 0; padding: 0; box-shadow: none; }
}
"""


def book_css(senders=(), styles=None) -> str:
	"""The stylesheet saved next to the chapters, for reading them in a browser."""
	styles = styles or load_styles()
	rules = [
		f"body div.email[data-from={_css_string(sender_key(s['name']))}] "
		f"{{ --sender: {s['color']}; --sender-text: {text_color_on(s['color'])}; }}"
		for s in senders]
	return (
		_BOOK_CSS_BASE % {'width': styles['editor']['column_width_in']}
		+ to_css(styles, root='body', chrome=False) + '\n'.join(rules) + ('\n' if rules else ''))


if __name__ == '__main__':
	if len(sys.argv) != 2:
		sys.exit('usage: python bookdoc.py chapter.html')
	source = Path(sys.argv[1])
	target = source.with_suffix('.rtf')
	target.write_text(html_to_rtf(source.read_text(encoding='utf-8'), base_dir=source.parent), encoding='ascii', newline='\n')
	print(f'wrote {target}')
