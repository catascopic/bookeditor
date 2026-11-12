"""Import RTF chapters into the editor's format, and check them against the book's styles.

	python rtfimport.py chapter.rtf            writes chapter.html next to it, and reports
	python rtfimport.py chapter.rtf --check    reports only; writes nothing
	python rtfimport.py my-book-folder         every .rtf in the folder

Each RTF paragraph is compared with the styles in styles.json (font, size, alignment,
indents, and bold/italic where the style calls for them) and comes out one of three ways:

	matched   it fits a style exactly
	fixed     it nearly fits one; it is imported as that style and the difference is listed
	unknown   it fits nothing; it is imported as body text carrying a note, which the
	          editor shows in its problems list until you give it a style or dismiss it

Checking is importing without saving, so there is only one set of rules.

The RTF itself is read by the separate `rtfparser` project.  It is looked for as an
installed module, then in the folder named by the RTFPARSER_PATH environment variable,
then in a folder called rtfparser beside this project.
"""
import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import bookdoc

HERE = Path(__file__).parent
ALIGNMENTS = {'c': 'centered', 'r': 'right-aligned', 'j': 'justified'}
TWIPS_PER_INCH = 1440
INDENT_TOLERANCE = 40  # twips; about 1/36 inch
STYLE_NAMES = {
	'chapter': 'Chapter', 'section': 'Section', 'body': 'Body', 'journal': 'Journal',
	'code': 'Code block', 'email': 'Email',
}
DINKUS_CHARS = set('✻✽✱＊*⁂')


def load_parser():
	"""The rtfparser module, wherever it lives (see the top of this file)."""
	try:
		import rtfparser
	except ImportError:
		folder = Path(os.environ.get('RTFPARSER_PATH') or HERE.parent / 'rtfparser')
		if not (folder / 'rtfparser.py').exists():
			raise ImportError(
				f'The RTF parser was not found in {folder}. Set the RTFPARSER_PATH environment '
				'variable to the folder that holds rtfparser.py.') from None
		sys.path.insert(0, str(folder))
		import rtfparser
	# The parser turns \endash into a plain hyphen. Until that is fixed there, correct it
	# here, so en dashes in a chapter survive the import.
	if rtfparser.ESCAPE.get('endash') == '-':
		rtfparser.ESCAPE['endash'] = '\N{EN DASH}'
	return rtfparser


# ---------------------------------------------------------------- reading the RTF

@dataclass
class Run:
	text: str
	font: str
	size: float  # points
	bold: bool
	italic: bool
	picture: object = None  # an rtfparser Picture, for a run that is an embedded picture


@dataclass
class Para:
	number: int  # counting from 1, as a reader would count paragraphs in the RTF
	runs: list
	align: str
	indent: int  # twips
	first_line: int
	space_before: int

	def __post_init__(self):
		self.text = ''.join(run.text for run in self.runs)
		self.pictures = [run.picture for run in self.runs if run.picture is not None]
		self.blank = not self.text.strip() and not self.pictures
		# what most of the paragraph's text looks like
		weights = {}
		inked = [run for run in self.runs if run.text.strip()] or [run for run in self.runs if run.picture is None]
		for run in inked:
			weights[(run.font, run.size)] = weights.get((run.font, run.size), 0) + max(1, len(run.text.strip()))
		self.font, self.size = max(weights, key=weights.get) if weights else ('', 0)
		total = sum(len(run.text.strip()) for run in inked) or 1
		# "mostly", so a heading with one emphasised word still counts as the heading's style
		self.bold = sum(len(run.text.strip()) for run in inked if run.bold) / total > 0.5
		self.italic = sum(len(run.text.strip()) for run in inked if run.italic) / total > 0.5

	def excerpt(self, length=70):
		text = ' '.join(self.text.split())
		return text if len(text) <= length else text[:length - 1].rstrip() + '…'

	def describe(self):
		parts = [f'{self.font} {self.size:g}pt']
		if self.bold:
			parts.append('bold')
		if self.italic:
			parts.append('italic')
		look = ' '.join(parts) + f', {self.align}'
		if self.indent:
			look += f', indented {self.indent / TWIPS_PER_INCH:g}in'
		if self.first_line:
			look += f', first line indented {self.first_line / TWIPS_PER_INCH:g}in'
		return look


def read_paragraphs(path) -> list[Para]:
	rtf = load_parser()
	paragraphs = []

	class Collector(rtf.Handler):
		def __init__(self, doc):
			super().__init__(doc)
			self.runs = []

		def add(self, text, picture=None):
			prop, doc = self.prop, self._doc
			font = doc.fonts.get(prop.get('f', doc.deff))
			self.runs.append(Run(
				text, font.name if font else '', (prop.get('fs') or 24) / 2,
				bool(prop.get('b')), bool(prop.get('i')), picture))

		def write(self, text):
			self.add(text)

		def hyperlink(self, text, url):
			self.add(text)

		def picture(self, pic):
			self.add('', pic)

		def par(self):
			prop = self.prop
			paragraphs.append(Para(
				len(paragraphs) + 1, self.runs, ALIGNMENTS.get(prop.get('q'), 'left'),
				int(prop.get('li') or 0), int(prop.get('fi') or 0), int(prop.get('sb') or 0)))
			self.runs = []

		def page_break(self):
			pass

		def end_doc(self):
			if any(run.text.strip() or run.picture is not None for run in self.runs):
				self.par()  # text after the last \par

	rtf.Parser(Collector).parse(path)
	return paragraphs


# ---------------------------------------------------------------- the report

@dataclass
class Issue:
	number: int  # paragraph number in the RTF
	text: str  # how the paragraph starts
	message: str


@dataclass
class Report:
	paragraphs: int = 0  # paragraphs with something in them
	matched: int = 0
	blank_lines: int = 0  # empty paragraphs dropped, since spacing is automatic now
	fixed: list = field(default_factory=list)
	unknown: list = field(default_factory=list)

	def summary(self):
		# counted by paragraph: one paragraph can collect several notes
		unknown = {issue.number for issue in self.unknown}
		fixed = {issue.number for issue in self.fixed} - unknown
		exact = max(0, self.paragraphs - len(unknown) - len(fixed))
		parts = [f'{exact} match']
		if fixed:
			parts.append(f'{len(fixed)} normalised')
		parts.append('1 needs a style' if len(unknown) == 1 else f'{len(unknown)} need a style')
		return f"{self.paragraphs} paragraph{'' if self.paragraphs == 1 else 's'}: {', '.join(parts)}"

	def to_dict(self):
		return {
			'summary': self.summary(), 'paragraphs': self.paragraphs, 'matched': self.matched,
			'blank_lines': self.blank_lines,
			'fixed': [vars(issue) for issue in self.fixed],
			'unknown': [vars(issue) for issue in self.unknown],
		}


# ---------------------------------------------------------------- RTF paragraphs -> blocks

class Importer:
	"""Turns RTF paragraphs into the editor's blocks (the same shapes bookdoc.parse_html gives).

	store_picture(data, kind) saves an embedded picture and returns the src to use for it;
	leave it out to check a file without saving anything.
	"""

	def __init__(self, styles=None, store_picture=None):
		self.styles = styles or bookdoc.load_styles()
		self.kinds = self.styles['blocks']
		self.code_font = self.styles['inline_code']['font']
		self.store_picture = store_picture
		email = self.kinds['email']
		label = lambda key: re.escape(email[key].strip()) or r'(?!)'  # an empty label never matches
		# "From: ALICE" alone, or "From: ALICE   To: BOB" on one line; and "To: BOB" alone
		self.from_line = re.compile(rf"^\s*{label('from_label')}\s*(.*?)(?:\s+{label('to_label')}\s*(.*?))?\s*$", re.I | re.S)
		self.to_line = re.compile(rf"^\s*{label('to_label')}\s*(.*?)\s*$", re.I | re.S)

	def run(self, paragraphs):
		self.blocks, self.report = [], Report()
		self.paragraphs = paragraphs
		self.report.paragraphs = sum(1 for p in paragraphs if not p.blank)
		self.joinable = False  # may the next paragraph continue the block before it?
		i = 0
		while i < len(paragraphs):
			para = paragraphs[i]
			if para.blank:
				self.blank(i)
				i += 1
			elif para.pictures:
				i = self.figure(i)
			elif self.is_dinkus(para):
				self.add({'type': 'dinkus'}, para)
				i += 1
			elif self.is_email_header(para):
				i = self.email(i)
			else:
				i = self.text_block(i)
		return self.blocks, self.report

	# -- matching a paragraph to a style

	def differences(self, para, kind):
		"""How para departs from a style: (penalty, [descriptions]). No penalty is an exact match."""
		style = self.kinds[kind]
		penalty, found = 0, []
		if para.font != style['font']:
			penalty += 3
			found.append(f"font is {para.font}, not {style['font']}")
		if para.size != style['size']:
			penalty += 2
			found.append(f"size is {para.size:g}pt, not {style['size']:g}pt")
		wanted = 'centered' if style.get('align') == 'center' else 'left'
		if para.align != wanted:
			penalty += 2
			found.append(f'{para.align}, not {wanted}')
		for value, key, name in ((para.indent, 'indent_in', 'indent'), (para.first_line, 'first_line_in', 'first-line indent')):
			inches = style.get(key) or 0
			if abs(value - inches * TWIPS_PER_INCH) > INDENT_TOLERANCE:
				penalty += 1
				found.append(f'{name} is {value / TWIPS_PER_INCH:g}in, not {inches:g}in')
		for flag in ('bold', 'italic'):  # only where the style itself is bold or italic
			if style.get(flag) and not getattr(para, flag):
				penalty += 1
				found.append(f'not {flag}')
		return penalty, found

	def classify(self, para):
		"""(kind, 'matched' | 'fixed' | 'unknown', [differences from that kind])"""
		ranked = sorted((self.differences(para, kind) + (kind,) for kind in STYLE_NAMES), key=lambda r: r[0])
		penalty, found, kind = ranked[0]
		return kind, ('matched' if penalty == 0 else 'fixed' if penalty <= 2 and para.font == self.kinds[kind]['font'] else 'unknown'), found

	def is_dinkus(self, para):
		inked = set(para.text) - set(' \t\n\N{NO-BREAK SPACE}')
		return bool(inked) and inked <= DINKUS_CHARS and para.align == 'centered'

	def is_email_header(self, para):
		email = self.kinds['email']
		return para.font in (email.get('header_font'), email['font']) and bool(self.from_line.match(para.text)) \
			and bool(email['from_label'].strip())

	# -- inline formatting

	def inline(self, para, font, bold=False, italic=False, runs=None):
		"""A paragraph's text as (text, marks) runs, relative to the style it is going into."""
		out, strays = [], set()
		for run in para.runs if runs is None else runs:
			if not run.text:
				continue
			marks = set()
			if run.font == self.code_font and font != self.code_font and run.text.strip():
				marks.add('code')  # in the editor inline code carries no other formatting
			else:
				if run.bold and not bold:
					marks.add('b')
				if run.italic != bool(italic):
					marks.add('i')  # in an italic style, upright text is the emphasis
				if run.font != font and any(c.isalnum() for c in run.text):
					strays.add(run.font)
			marks = frozenset(marks if run.text.strip() else ())
			if out and out[-1][1] == marks:
				out[-1] = (out[-1][0] + run.text, marks)
			else:
				out.append((run.text, marks))
		for stray in sorted(strays):
			self.fixed(para, f'Some of the text was in {stray}; it is now in {font} like the rest.')
		return out

	# -- building blocks

	def add(self, block, para, counted=True):
		self.blocks.append(block)
		self.joinable = True
		if counted:
			self.report.matched += 1

	def fixed(self, para, message):
		self.report.fixed.append(Issue(para.number, para.excerpt(), message))

	def blank(self, i):
		self.report.blank_lines += 1
		self.joinable = False
		before = next((p for p in reversed(self.paragraphs[:i]) if not p.blank), None)
		after = next((p for p in self.paragraphs[i + 1:] if not p.blank), None)
		follows_blank = i > 0 and self.paragraphs[i - 1].blank
		if before and after and not follows_blank and all(
				self.classify(p)[0] == 'body' and not p.pictures and not self.is_dinkus(p) for p in (before, after)):
			self.fixed(after, 'A blank line before this paragraph was removed. If it marked a scene break, add a dinkus.')

	def text_block(self, i):
		para = self.paragraphs[i]
		kind, verdict, found = self.classify(para)
		style = self.kinds[kind]
		if verdict == 'unknown' or (kind == 'email'):
			# Email text with no From line above it has nowhere to go either.
			why = (f'Was {para.describe()}.' if verdict == 'unknown' else
				'Looks like email text, but there is no From line above it.')
			note = f'{why} Closest style: {STYLE_NAMES[kind]}.'
			body = self.kinds['body']
			self.blocks.append({
				'type': 'body', 'note': note,
				'runs': self.inline(para, para.font, para.bold, para.italic)})
			self.joinable = False
			self.report.unknown.append(Issue(para.number, para.excerpt(), note))
			return i + 1
		if verdict == 'fixed':
			self.fixed(para, f"Imported as {STYLE_NAMES[kind]}, though it differed: {'; '.join(found)}.")
		else:
			self.report.matched += 1
		last = self.blocks[-1] if self.blocks and self.joinable else None
		if kind == 'code':
			if last and last['type'] == 'code':  # a program typed as one paragraph per line
				last['runs'] = [(last['runs'][0][0] + '\n' + para.text, frozenset())]
			else:
				self.add({'type': 'code', 'runs': [(para.text, frozenset())]}, para, counted=False)
		elif kind == 'journal':
			runs = self.inline(para, style['font'])
			if last and last['type'] == 'journal':  # a blank line is what separates two entries
				last['paras'].append(runs)
			else:
				self.add({'type': 'journal', 'paras': [runs]}, para, counted=False)
		else:
			self.add({'type': kind, 'runs': self.inline(para, style['font'], style.get('bold'), style.get('italic'))}, para, counted=False)
		self.joinable = True
		return i + 1

	def email(self, i):
		paragraphs, style = self.paragraphs, self.kinds['email']
		head = paragraphs[i]
		sender, recipient = self.from_line.match(head.text).groups()
		i += 1
		if recipient is None and i < len(paragraphs) and self.to_line.match(paragraphs[i].text) \
				and paragraphs[i].font in (style.get('header_font'), style['font']):
			recipient = self.to_line.match(paragraphs[i].text).group(1)
			self.report.matched += 1
			i += 1
		block = {
			'type': 'email',
			'from': [(sender, frozenset())] if sender else [],
			'to': [(recipient, frozenset())] if recipient else [],
			'paras': []}
		# the email's text: paragraphs in the email font, allowing blank lines between them
		while i < len(paragraphs):
			j = i
			while j < len(paragraphs) and paragraphs[j].blank:
				j += 1
			if j == len(paragraphs) or paragraphs[j].font != style['font'] or paragraphs[j].pictures \
					or self.is_email_header(paragraphs[j]):
				break
			self.report.blank_lines += j - i
			para = paragraphs[j]
			penalty, found = self.differences(para, 'email')
			if penalty:
				self.fixed(para, f"Imported as Email text, though it differed: {'; '.join(found)}.")
			else:
				self.report.matched += 1
			block['paras'].append(self.inline(para, style['font']))
			i = j + 1
		if not block['paras']:
			block['paras'].append([])
		self.add(block, head)
		return i

	def figure(self, i):
		para, style, body = self.paragraphs[i], self.kinds['figure'], self.kinds['body']
		caption = self.inline(para, body['font']) if para.text.strip() else []
		i += 1
		# a centred line of body text straight after a lone picture is its caption
		if not caption and len(para.pictures) == 1 and i < len(self.paragraphs):
			after = self.paragraphs[i]
			if not after.blank and not after.pictures and after.align == 'centered' \
					and (after.font, after.size) == (body['font'], body['size']) and not self.is_dinkus(after):
				caption = self.inline(after, body['font'])
				self.report.paragraphs -= 1  # picture and caption count as one
				i += 1
		for n, picture in enumerate(para.pictures):
			info = bookdoc.image_info(picture.data or b'')
			if not info:
				note = (f'A picture here could not be imported: it is in {picture.format or "an unknown"} format, '
					'and the editor takes PNG and JPEG. Add it again with the Picture button.')
				self.blocks.append({'type': 'body', 'note': note, 'runs': [('[picture]', frozenset())]})
				self.report.unknown.append(Issue(para.number, '[picture]', note))
				continue
			src = self.store_picture(picture.data, info[0]) if self.store_picture else 'images/(not saved)'
			last = n == len(para.pictures) - 1
			self.blocks.append({'type': 'figure', 'src': src, 'runs': caption if last else []})
		if para.align != 'centered':
			self.fixed(para, 'A picture that was not centred is now centred.')
		else:
			self.report.matched += 1
		self.joinable = False
		return i


def import_rtf(path, styles=None, store_picture=None):
	"""Read an RTF file. Returns (blocks, Report); see Importer."""
	return Importer(styles, store_picture).run(read_paragraphs(path))


# ---------------------------------------------------------------- command line

def print_report(name, report, out=sys.stdout):
	print(f'{name}: {report.summary()}', file=out)
	for title, issues in (('Need a style', report.unknown), ('Normalised', report.fixed)):
		if issues:
			print(f'  {title}:', file=out)
		for issue in issues:
			print(f'    paragraph {issue.number}: "{issue.text}"', file=out)
			print(f'      {issue.message}', file=out)
	if report.blank_lines:
		print(f"  {report.blank_lines} blank line{'' if report.blank_lines == 1 else 's'} dropped (spacing is automatic).", file=out)


def main(argv=None):
	parser = argparse.ArgumentParser(description='Import RTF chapters into the book editor, or check them against its styles.')
	parser.add_argument('paths', nargs='+', help='.rtf files, or folders of them')
	parser.add_argument('--check', action='store_true', help='report only; write nothing')
	parser.add_argument('--out', help='folder to write the .html files to (default: next to each .rtf)')
	parser.add_argument('--force', action='store_true', help='overwrite .html files that already exist')
	args = parser.parse_args(argv)
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

	files = []
	for path in map(Path, args.paths):
		files += sorted(path.glob('*.rtf')) if path.is_dir() else [path]
	if not files:
		sys.exit('no .rtf files found')
	clean = True
	for source in files:
		folder = Path(args.out) if args.out else source.parent
		target = folder / (source.stem + '.html')
		if not args.check and target.exists() and not args.force:
			print(f'{source.name}: skipped, {target} already exists (use --force to replace it)')
			clean = False
			continue
		store = None if args.check else (
			lambda data, kind: 'images/' + bookdoc.store_image(folder / 'images', source.stem, data))
		try:
			blocks, report = import_rtf(source, store_picture=store)
		except Exception as error:  # a file the parser can't read shouldn't stop the others
			print(f'{source.name}: could not be read ({type(error).__name__}: {error})')
			clean = False
			continue
		print_report(source.name, report)
		clean = clean and not report.unknown
		if not args.check:
			folder.mkdir(parents=True, exist_ok=True)
			target.write_text(bookdoc.wrap_document(bookdoc.blocks_to_html(blocks), source.stem), encoding='utf-8', newline='\n')
			print(f'  wrote {target}')
	return 0 if clean else 1


if __name__ == '__main__':
	sys.exit(main())
