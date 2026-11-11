"""The book document format: read the saved HTML, and turn it into RTF or editor CSS.

A document on disk is plain HTML with one block per line, using only these shapes:

	<h1>…</h1>                      chapter header
	<h2>…</h2>                      section header
	<p>…</p>                        body paragraph
	<hr>                            dinkus
	<pre><code>…</code></pre>       code block
	<div class="journal"><p>…</p>…</div>
	<div class="email"><div class="email-from">…</div><div class="email-to">…</div><p>…</p>…</div>

Inline: <strong>, <em>, <code> (inline code) and <br>.

Can also be run directly:  python bookdoc.py chapter.html  ->  writes chapter.rtf
"""
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

STYLES_PATH = Path(__file__).parent / 'styles.json'

# A run is (text, marks) where marks is a frozenset drawn from {'b', 'i', 'code'}.
MARK_TAGS = {'strong': 'b', 'b': 'b', 'em': 'i', 'i': 'i', 'code': 'code'}
TEXT_TAGS = {'h1': 'chapter', 'h2': 'section', 'p': 'para', 'pre': 'code'}


def load_styles():
	return json.loads(STYLES_PATH.read_text(encoding='utf-8'))


# ---------------------------------------------------------------- HTML -> blocks

class _DocParser(HTMLParser):
	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.blocks = []
		self.container = None  # the email/journal block being filled, if any
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
		elif tag == 'hr':
			self.blocks.append({'type': 'dinkus'})
		elif tag == 'br':
			self.handle_data('\n')
		elif tag in MARK_TAGS and self.kind != 'code':
			self.marks.append(MARK_TAGS[tag])

	def handle_endtag(self, tag):
		if tag in TEXT_TAGS or (tag == 'div' and self.runs is not None):
			self._close()
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
		if kind in ('from', 'to'):
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


_BLOCK_BREAK = re.compile(r'(</p>|</h1>|</h2>|</pre>|</div>|<hr[^>]*>|<div class="(?:email|journal)">)(?!\n)')


def tidy_html(html: str) -> str:
	"""Put each block on its own line so the saved file reads and diffs well."""
	return _BLOCK_BREAK.sub(r'\1\n', html.strip()) if html.strip() else '<p></p>\n'


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


class _RtfBuilder:
	def __init__(self, styles):
		self.styles = styles
		self.blocks = styles['blocks']
		self.fonts = []
		self.out = []

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

	def para(self, style, content, before=0, after=0, font=None, bold=None):
		bold = style.get('bold') if bold is None else bold
		codes = ['\\pard']
		if style.get('keep_with_next'):
			codes.append('\\keepn')
		codes.append('\\nowidctlpar')
		for word, value in (
			('fi', _twips_in(style.get('first_line_in'))),
			('li', _twips_in(style.get('indent_in'))),
			('sb', _twips_pt(before)),
			('sa', _twips_pt(after)),
		):
			if value:
				codes.append(f'\\{word}{value}')
		codes.append('\\sl240\\slmult1')
		if style.get('align') == 'center':
			codes.append('\\qc')
		codes.append(f"\\plain\\f{self.font(font or style['font'])}\\fs{round(style['size'] * 2)}")
		if bold:
			codes.append('\\b')
		if style.get('italic'):
			codes.append('\\i')
		self.out.append(f"{''.join(codes)} {content}\\par\n")

	def container(self, style, paras, first_before):
		"""Paragraphs of an email or journal: spaced apart, with the block's own space at the ends."""
		for i, runs in enumerate(paras):
			last = i == len(paras) - 1
			self.para(
				style, self.runs(runs, style),
				before=first_before if i == 0 else 0,
				after=style.get('space_after') if last else style.get('para_spacing'))

	def block(self, block):
		kind = block['type']
		style = self.blocks[kind]
		before, after = style.get('space_before'), style.get('space_after')
		if kind == 'dinkus':
			symbol = self.font(style.get('symbol_font') or style['font'])
			content = ''.join(c if c == ' ' else f'{{\\f{symbol} {_escape(c)}}}' for c in style['text'])
			self.para(style, content, before, after)
		elif kind == 'email':
			header = dict(font=style.get('header_font'), bold=style.get('header_bold'))
			self.para(style, _escape(style['from_label']) + self.runs(block['from'], style), before=before, **header)
			self.para(style, _escape(style['to_label']) + self.runs(block['to'], style), **header)
			self.container(style, block['paras'], first_before=0)
		elif kind == 'journal':
			self.container(style, block['paras'], first_before=before)
		else:
			self.para(style, self.runs(block['runs'], style), before, after)

	def document(self, blocks):
		for block in blocks:
			self.block(block)
		fonts = ''.join(f'{{\\f{i}\\fnil\\fcharset0 {name};}}' for i, name in enumerate(self.fonts))
		return (
			'{\\rtf1\\ansi\\ansicpg1252\\deff0\\deflang1033'
			f'{{\\fonttbl{fonts}}}\n'
			'{\\*\\generator bookeditor;}\\viewkind4\\uc1\n'
			+ ''.join(self.out) + '}\n')


def to_rtf(blocks: list[dict], styles=None) -> str:
	return _RtfBuilder(styles or load_styles()).document(blocks)


def html_to_rtf(html: str, styles=None) -> str:
	return to_rtf(parse_html(html), styles)


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


def to_css(styles=None) -> str:
	styles = styles or load_styles()
	b = styles['blocks']
	root = '.ProseMirror'
	rules = [f".page {{ width: {styles['editor']['column_width_in']}in; }}"]
	for selector, kind in (('h1', 'chapter'), ('h2', 'section'), ('> p', 'body'), ('pre', 'code'), ('hr', 'dinkus')):
		rules.append(f'{root} {selector} {{ {_css_text(b[kind])}{_css_box(b[kind])}}}')
	rules.append(f"{root} hr::before {{ content: {_css_string(b['dinkus']['text'])}; }}")
	rules.append(f"{root} h2 em {{ font-style: {'normal' if b['section'].get('italic') else 'italic'}; }}")
	inline_code = styles['inline_code']
	inline_code_css = f"font-family: {_family(inline_code['font'])}; font-weight: {'bold' if inline_code.get('bold') else 'inherit'};"
	rules.append(f"{root} :not(pre) > code {{ {inline_code_css} }}")
	for kind in ('email', 'journal'):
		s = b[kind]
		rules.append(f'{root} div.{kind} {{ {_css_text(s)}{_css_box(s)}}}')
		rules.append(f"{root} div.{kind} > p {{ margin-bottom: {s.get('para_spacing', 0)}pt; }}")
		rules.append(f'{root} div.{kind} > p:last-child {{ margin-bottom: 0; }}')
	e = b['email']
	rules.append(f"{root} .email-from, {root} .email-to {{ {_css_text(e, e.get('header_font'), e.get('header_bold'))}}}")
	rules.append(f"{root} .email-from::before {{ content: {_css_string(e['from_label'])}; }}")
	rules.append(f"{root} .email-to::before {{ content: {_css_string(e['to_label'])}; }}")
	# toolbar buttons are drawn in the font of the thing they produce
	for kind in ('body', 'chapter', 'section', 'email', 'journal', 'code'):
		s = b[kind]
		rules.append(
			f".toolbar [data-block=\"{kind}\"] {{ font-family: {_family(s['font'])}; "
			f"font-weight: {'bold' if s.get('bold') else 'normal'}; font-style: {'italic' if s.get('italic') else 'normal'}; }}")
	rules.append(f".toolbar [data-mark=\"bold\"], .toolbar [data-mark=\"italic\"] {{ font-family: {_family(b['body']['font'])}; }}")
	rules.append(f".toolbar [data-mark=\"code\"] {{ {inline_code_css} }}")
	return '\n'.join(rules) + '\n'


if __name__ == '__main__':
	if len(sys.argv) != 2:
		sys.exit('usage: python bookdoc.py chapter.html')
	source = Path(sys.argv[1])
	target = source.with_suffix('.rtf')
	target.write_text(html_to_rtf(source.read_text(encoding='utf-8')), encoding='ascii', newline='\n')
	print(f'wrote {target}')
