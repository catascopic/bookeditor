import { Editor, Extension, Node } from '@tiptap/core'
import { StarterKit } from '@tiptap/starter-kit'
import { Plugin, Selection, TextSelection } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

// ---------------------------------------------------------------- custom block types
//
// Built-in TipTap pieces cover: paragraph (body), heading 1/2 (chapter/section),
// horizontal rule (dinkus), code block, bold, italic, inline code.
// Journal entries and quoted emails are defined here.

const Journal = Node.create({
	name: 'journal',
	group: 'block',
	content: 'paragraph+',
	defining: true,
	parseHTML: () => [{ tag: 'div.journal' }],
	renderHTML: () => ['div', { class: 'journal' }, 0],
})

// From/To are single lines of plain text. Enter or Tab moves to the next part of the email.
function emailField(name, cssClass) {
	return Node.create({
		name,
		content: 'text*',
		marks: '',
		defining: true,
		isolating: true,
		parseHTML: () => [{ tag: `div.${cssClass}` }],
		renderHTML: () => ['div', { class: cssClass }, 0],
		addKeyboardShortcuts() {
			const next = () => {
				const { $from } = this.editor.state.selection
				if ($from.parent.type.name !== name) return false
				return this.editor.commands.setTextSelection($from.after() + 1)
			}
			// Arrows past the end of From or To go to the start of the next part, rather than
			// onto the gap between the two (a horizontal cursor that typing can't use).
			const forward = () => {
				const { selection } = this.editor.state
				const { $from } = selection
				if (!selection.empty || $from.parent.type.name !== name) return false
				if ($from.parentOffset < $from.parent.content.size) return false
				return this.editor.commands.setTextSelection($from.after() + 1)
			}
			const back = () => {
				const { selection } = this.editor.state
				const { $from } = selection
				if (!selection.empty || $from.parent.type.name !== name || $from.parentOffset > 0) return false
				if ($from.index($from.depth - 1) === 0) return false // the top of the email: leave it as it is
				return this.editor.commands.setTextSelection($from.before() - 1)
			}
			return {
				Enter: next,
				Tab: next,
				ArrowRight: forward,
				ArrowDown: forward,
				ArrowLeft: back,
				ArrowUp: back,
			}
		},
	})
}

const Email = Node.create({
	name: 'email',
	group: 'block',
	content: 'emailFrom emailTo paragraph+',
	defining: true,
	parseHTML: () => [{ tag: 'div.email' }],
	renderHTML: () => ['div', { class: 'email' }, 0],
	addKeyboardShortcuts() {
		return {
			// Backspace at the very start of From, To or the first body paragraph would otherwise
			// try to merge the pieces of the email together. Delete the email if it is empty,
			// and do nothing otherwise.
			Backspace: () => {
				const { selection } = this.editor.state
				const { $from } = selection
				if (!selection.empty || $from.parentOffset > 0 || $from.depth < 2) return false
				const email = $from.node($from.depth - 1)
				if (email.type.name !== 'email' || $from.index($from.depth - 1) > 2) return false
				if (email.textContent === '') {
					const pos = $from.before($from.depth - 1)
					return this.editor.commands.command(({ tr }) => {
						tr.replaceWith(pos, pos + email.nodeSize, this.editor.schema.nodes.paragraph.create())
						tr.setSelection(TextSelection.create(tr.doc, pos + 1))
						return true
					})
				}
				return true
			},
		}
	},
})

// ---------------------------------------------------------------- email sender colours
//
// Senders and their colours are a setting of the documents folder (senders.json), not of
// any one document. An email is coloured by looking its From line up in that list, so
// nothing about colour is stored in the document itself.

let senders = [] // [{ name, color }]

const senderKey = name => name.trim().split(/\s+/).join(' ').toLowerCase()

// White or black, whichever reads better on the colour (same rule as bookdoc.text_color_on).
function textColorOn(color) {
	const [r, g, b] = [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16) / 255)
	return 0.299 * r + 0.587 * g + 0.114 * b > 0.6 ? '#000000' : '#ffffff'
}

const SenderColors = Extension.create({
	name: 'senderColors',
	addProseMirrorPlugins() {
		return [new Plugin({
			props: {
				decorations(state) {
					const colors = new Map(senders.map(sender => [senderKey(sender.name), sender.color]))
					const decorations = []
					state.doc.forEach((node, pos) => {
						if (node.type.name !== 'email') return
						const color = colors.get(senderKey(node.firstChild.textContent))
						if (!color) return
						decorations.push(Decoration.node(pos, pos + node.nodeSize, {
							style: `--sender: ${color}; --sender-text: ${textColorOn(color)}`,
						}))
					})
					return DecorationSet.create(state.doc, decorations)
				},
			},
		})]
	},
})

async function loadSenders() {
	try {
		senders = (await (await api('GET', '/api/senders')).json()).senders
	} catch {
		senders = []
	}
}

// The Senders dialog: one row per sender, with a colour and the name as written in From.
function senderRow(sender) {
	const row = document.createElement('div')
	row.className = 'sender-row'
	const color = Object.assign(document.createElement('input'), { type: 'color', value: sender.color, title: 'Colour' })
	const name = Object.assign(document.createElement('input'), {
		type: 'text', value: sender.name, placeholder: 'Name, as written after From:', spellcheck: false,
	})
	const preview = Object.assign(document.createElement('span'), { className: 'sender-preview' })
	const paint = () => {
		preview.textContent = `From: ${name.value || '…'}`
		preview.style.background = color.value
		preview.style.color = textColorOn(color.value)
	}
	color.addEventListener('input', paint)
	name.addEventListener('input', paint)
	paint()
	const remove = Object.assign(document.createElement('button'), { type: 'button', textContent: 'Remove' })
	remove.addEventListener('click', () => row.remove())
	row.append(color, name, preview, remove)
	return row
}

// Names used in From lines of the open document that have no colour yet: offered as rows to fill in.
function unlistedSenders() {
	const known = new Set(senders.map(sender => senderKey(sender.name)))
	const names = []
	editor.state.doc.forEach(node => {
		if (node.type.name !== 'email') return
		const name = node.firstChild.textContent.trim()
		if (name && !known.has(senderKey(name))) {
			known.add(senderKey(name))
			names.push(name)
		}
	})
	return names
}

const NEW_SENDER_COLORS = ['#b3261e', '#1a5fb4', '#26734d', '#8e44ad', '#b25e00', '#0e7c86', '#a3195b', '#5c6b1e']

function openSenders() {
	const rows = $('sender-rows')
	const fresh = unlistedSenders().map((name, i) => ({
		name, color: NEW_SENDER_COLORS[(senders.length + i) % NEW_SENDER_COLORS.length],
	}))
	rows.replaceChildren(...[...senders, ...fresh].map(senderRow))
	$('senders-dialog').returnValue = '' // so Escape after an earlier Save doesn't save again
	$('senders-dialog').showModal()
}

async function saveSenders() {
	const list = [...document.querySelectorAll('#sender-rows .sender-row')]
		.map(row => ({ color: row.children[0].value, name: row.children[1].value.trim() }))
		.filter(sender => sender.name)
	try {
		const response = await fetch('/api/senders', {
			method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(list),
		})
		if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
		senders = (await response.json()).senders
		editor.view.dispatch(editor.state.tr) // redraw the emails in their new colours
		say('Senders saved')
	} catch (error) {
		say(`Could not save senders: ${error.message}`, true)
	}
}

// A picture with a caption. The caption is the node's text; the picture is an attribute.
const Figure = Node.create({
	name: 'figure',
	group: 'block',
	content: 'inline*',
	defining: true,
	isolating: true,
	addAttributes() {
		return { src: { default: null } }
	},
	parseHTML: () => [{
		tag: 'figure',
		contentElement: 'figcaption',
		getAttrs: element => ({ src: element.querySelector('img')?.getAttribute('src') }),
	}],
	renderHTML: ({ node }) => ['figure', ['img', { src: node.attrs.src }], ['figcaption', 0]],
	// The caption is optional. While it is empty and the cursor is elsewhere it takes up no
	// room; clicking the picture puts the cursor in it, which opens it up for typing.
	addNodeView() {
		return ({ node, editor, getPos }) => {
			const dom = document.createElement('figure')
			const frame = document.createElement('div')
			frame.className = 'figure-frame'
			frame.contentEditable = 'false'
			const image = document.createElement('img')
			image.src = node.attrs.src
			image.draggable = false
			image.title = 'Click to add or edit the caption'
			const remove = Object.assign(document.createElement('button'), {
				type: 'button', className: 'figure-remove', textContent: 'Remove picture',
			})
			frame.append(image, remove)
			const caption = document.createElement('figcaption')
			dom.append(frame, caption)

			const current = () => editor.state.doc.nodeAt(getPos())
			const toCaption = event => {
				event.preventDefault()
				editor.chain().focus().setTextSelection(getPos() + 1 + current().content.size).run()
			}
			image.addEventListener('mousedown', toCaption)
			// A click on an empty caption's prompt would otherwise land beside it, not in it.
			caption.addEventListener('mousedown', event => {
				if (current().content.size === 0) toCaption(event)
			})
			remove.addEventListener('mousedown', event => event.preventDefault())
			remove.addEventListener('click', () => {
				const pos = getPos()
				editor.chain().focus().deleteRange({ from: pos, to: pos + current().nodeSize }).run()
			})
			return {
				dom,
				contentDOM: caption,
				stopEvent: event => frame.contains(event.target), // the picture and its button are ours
				ignoreMutation: mutation => !caption.contains(mutation.target),
			}
		}
	},
	// Marks the picture the cursor is in, so its caption line and Remove button can show.
	addProseMirrorPlugins() {
		return [new Plugin({
			props: {
				decorations(state) {
					const { $from } = state.selection
					if ($from.parent.type.name !== 'figure') return null
					const pos = $from.before()
					return DecorationSet.create(state.doc, [
						Decoration.node(pos, pos + $from.parent.nodeSize, { class: 'active' }),
					])
				},
			},
		})]
	},
	addKeyboardShortcuts() {
		const inCaption = () => this.editor.state.selection.$from.parent.type.name === 'figure'
		// The browser's own up/down can't step into a caption that is taking up no room, nor
		// past the picture above one, so the arrow keys are handled here: moving onto a
		// picture lands in its caption, and moving on from the caption leaves the picture.
		const arrow = direction => () => {
			const { state, view } = this.editor
			const { selection, doc } = state
			const { $from } = selection
			if (!selection.empty || $from.depth === 0) return false
			if (inCaption()) {
				const edge = doc.resolve(direction < 0 ? $from.before() : $from.after())
				view.dispatch(state.tr.setSelection(Selection.near(edge, direction)).scrollIntoView())
				return true
			}
			if (!view.endOfTextblock(direction < 0 ? 'up' : 'down')) return false
			const neighbour = doc.maybeChild($from.index(0) + direction)
			if (!neighbour || neighbour.type.name !== 'figure') return false
			const start = direction < 0 ? $from.before(1) - neighbour.nodeSize : $from.after(1)
			return this.editor.commands.setTextSelection(start + 1 + neighbour.content.size)
		}
		return {
			ArrowUp: arrow(-1),
			ArrowDown: arrow(1),
			// Enter leaves the caption for a new paragraph below (rather than splitting the picture in two).
			Enter: () => {
				if (!inCaption()) return false
				const after = this.editor.state.selection.$from.after()
				return this.editor.chain()
					.insertContentAt(after, { type: 'paragraph' })
					.setTextSelection(after + 1)
					.run()
			},
			// Backspace at the start of the caption removes the picture only once the caption is empty.
			Backspace: () => {
				const { selection } = this.editor.state
				if (!inCaption() || !selection.empty || selection.$from.parentOffset > 0) return false
				if (selection.$from.parent.content.size > 0) return true
				const pos = selection.$from.before()
				return this.editor.commands.command(({ tr }) => {
					tr.replaceWith(pos, pos + selection.$from.parent.nodeSize, this.editor.schema.nodes.paragraph.create())
					tr.setSelection(TextSelection.create(tr.doc, pos + 1))
					return true
				})
			},
		}
	},
})

// Upload a picture file to the documents folder, then put it in the document below the
// block the cursor is in (or in place of that block, if it is an empty paragraph).
async function addPicture(file) {
	let src
	try {
		const response = await api('POST', `/api/image?name=${encodeURIComponent(file.name || 'picture')}`, file)
		src = (await response.json()).src
	} catch (error) {
		say(error.message.startsWith('415') ? 'Only PNG and JPEG pictures can be used' : `Could not add picture: ${error.message}`, true)
		return false
	}
	const { state } = editor
	const { $from } = state.selection
	const figure = state.schema.nodes.figure.create({ src })
	return editor.chain().focus().command(({ tr }) => {
		const top = $from.depth > 0 ? $from.node(1) : null
		let pos
		if ($from.depth === 1 && top.type.name === 'paragraph' && top.content.size === 0) {
			pos = $from.before(1)
			tr.replaceWith(pos, pos + top.nodeSize, figure)
		} else {
			pos = top ? $from.after(1) : state.selection.to
			tr.insert(pos, figure)
		}
		tr.setSelection(TextSelection.create(tr.doc, pos + 1)) // cursor in the caption
		return true
	}).run()
}

function pictureFiles(dataTransfer) {
	return [...(dataTransfer?.files || [])].filter(file => file.type.startsWith('image/'))
}

// ---------------------------------------------------------------- block commands

function enclosing(state, typeName) {
	const { $from } = state.selection
	for (let depth = $from.depth; depth > 0; depth--) {
		if ($from.node(depth).type.name === typeName) return { node: $from.node(depth), pos: $from.before(depth) }
	}
	return null
}

// Turn the selected body paragraphs into an email (adding From/To), or an email back into paragraphs.
function toggleEmail(editor) {
	const { state } = editor
	const inside = enclosing(state, 'email')
	if (inside) {
		const paragraphs = []
		inside.node.forEach(child => { if (child.type.name === 'paragraph') paragraphs.push(child) })
		return editor.chain().focus().command(({ tr }) => {
			tr.replaceWith(inside.pos, inside.pos + inside.node.nodeSize, paragraphs)
			tr.setSelection(TextSelection.create(tr.doc, inside.pos + 1))
			return true
		}).run()
	}
	const { $from, $to } = state.selection
	const range = $from.blockRange($to)
	if (!range || range.depth !== 0) return false
	const { email, emailFrom, emailTo, paragraph } = state.schema.nodes
	const paragraphs = []
	for (let i = range.startIndex; i < range.endIndex; i++) {
		const child = range.parent.child(i)
		if (!child.isTextblock) return false // pictures, dinkuses and the like can't go inside an email
		// an email holds only paragraphs, so a chapter header, section header or code block becomes one
		paragraphs.push(child.type === paragraph ? child : paragraph.create(null, child.content))
	}
	const node = email.create(null, [emailFrom.create(), emailTo.create(), ...paragraphs])
	return editor.chain().focus().command(({ tr }) => {
		tr.replaceWith(range.start, range.end, node)
		tr.setSelection(TextSelection.create(tr.doc, range.start + 2)) // cursor in From
		return true
	}).run()
}

const BLOCKS = {
	body: {
		label: 'Body',
		active: e => e.isActive('paragraph') && !e.isActive('journal') && !e.isActive('email'),
		run: e => {
			if (e.isActive('email')) return toggleEmail(e)
			if (e.isActive('journal')) return e.chain().focus().lift('journal').run()
			return e.chain().focus().setParagraph().run()
		},
	},
	chapter: {
		label: 'Chapter header',
		active: e => e.isActive('heading', { level: 1 }),
		run: e => e.chain().focus().toggleHeading({ level: 1 }).run(),
	},
	section: {
		label: 'Section header',
		active: e => e.isActive('heading', { level: 2 }),
		run: e => e.chain().focus().toggleHeading({ level: 2 }).run(),
	},
	email: {
		label: 'Email',
		active: e => e.isActive('email'),
		run: toggleEmail,
	},
	journal: {
		label: 'Journal entry',
		active: e => e.isActive('journal'),
		run: e => e.chain().focus().toggleWrap('journal').run(),
	},
	code: {
		label: 'Code block',
		active: e => e.isActive('codeBlock'),
		run: e => e.chain().focus().toggleCodeBlock().run(),
	},
	figure: {
		label: 'Picture',
		active: e => e.isActive('figure'),
		run: () => $('picture-file').click(),
	},
	dinkus: {
		label: 'Dinkus',
		active: e => e.isActive('horizontalRule'),
		run: e => e.chain().focus().setHorizontalRule().run(),
	},
}

// ---------------------------------------------------------------- import notes
//
// The RTF importer leaves a note on any paragraph it could not match to a style
// (<p data-note="…">). The notes are listed beside the page until each paragraph is given
// a style or its note is dismissed. They are saved with the document, never exported.

const Notes = Extension.create({
	name: 'notes',
	addGlobalAttributes() {
		return [{
			types: ['paragraph'],
			attributes: {
				note: {
					default: null,
					keepOnSplit: false,
					parseHTML: element => element.getAttribute('data-note'),
					renderHTML: attributes => (attributes.note ? { 'data-note': attributes.note } : {}),
				},
			},
		}]
	},
})

function notedParagraphs() {
	const found = []
	editor.state.doc.descendants((node, pos) => {
		if (node.type.name === 'paragraph' && node.attrs.note) found.push({ pos, note: node.attrs.note, text: node.textContent })
	})
	return found
}

function clearNotes(from, to) {
	const { state } = editor
	const tr = state.tr
	state.doc.nodesBetween(from, to, (node, pos) => {
		if (node.type.name === 'paragraph' && node.attrs.note) tr.setNodeAttribute(pos, 'note', null)
	})
	if (tr.docChanged) editor.view.dispatch(tr)
}

// Choosing a style for a paragraph (Body included) answers its note.
function applyStyle(key) {
	const { from, to } = editor.state.selection
	clearNotes(from, to)
	return BLOCKS[key].run(editor)
}

let problemsShown = ''
let importReport = null

function refreshProblems() {
	const noted = notedParagraphs()
	const key = JSON.stringify(noted) + (importReport ? 'r' : '')
	if (key === problemsShown) return
	problemsShown = key
	$('problems-unknown').hidden = !noted.length
	$('problems-count').textContent = noted.length ? `(${noted.length})` : ''
	$('problem-list').replaceChildren(...noted.map((item, index) => {
		const row = document.createElement('li')
		const go = Object.assign(document.createElement('button'), { type: 'button', className: 'problem-go' })
		const excerpt = Object.assign(document.createElement('strong'), { textContent: item.text.slice(0, 80) || '(empty paragraph)' })
		const note = Object.assign(document.createElement('span'), { textContent: item.note })
		go.append(excerpt, note)
		// positions are looked up afresh on click: the list can be older than the last keystroke
		go.addEventListener('click', () => {
			const target = notedParagraphs()[index]
			if (target) editor.chain().focus().setTextSelection(target.pos + 1).scrollIntoView().run()
		})
		const dismiss = Object.assign(document.createElement('button'), { type: 'button', textContent: 'Dismiss', title: 'Keep it as body text' })
		dismiss.addEventListener('click', () => {
			const target = notedParagraphs()[index]
			if (target) clearNotes(target.pos, target.pos + 1)
		})
		row.append(go, dismiss)
		return row
	}))
	$('import-report').hidden = !importReport
	$('problems').hidden = !noted.length && !importReport
}

function showImportReport(report) {
	importReport = report
	if (report) {
		const blanks = report.blank_lines ? ` Blank lines dropped: ${report.blank_lines} (spacing is automatic).` : ''
		$('import-summary').textContent = `${report.summary}.${blanks}`
		$('import-fixed').replaceChildren(...report.fixed.map(issue => {
			const row = document.createElement('li')
			const excerpt = Object.assign(document.createElement('strong'), { textContent: issue.text })
			const message = Object.assign(document.createElement('span'), { textContent: issue.message })
			row.append(excerpt, message)
			return row
		}))
	}
	problemsShown = ''
	if (editor) refreshProblems()
}

async function importRtf(file) {
	if (dirty && !confirm('Discard unsaved changes?')) return
	const files = await refreshFiles()
	let name = file.name.replace(/\.rtf$/i, '')
	for (;;) {
		name = prompt('Name for the imported document:', name)
		if (!name) return
		name = name.trim().replace(/\.html$/i, '')
		if (!/^[\w\- .()]+$/.test(name)) alert('Use only letters, numbers, spaces, and - _ . ( ) in the name.')
		else if (files.includes(name + '.html')) alert(`"${name}" already exists. Choose another name.`)
		else break
	}
	let result
	try {
		const response = await fetch(`/api/import?name=${encodeURIComponent(name)}`, { method: 'POST', body: file })
		result = await response.json()
		if (!response.ok) throw new Error(result.error || response.statusText)
	} catch (error) {
		return say(`Could not import ${file.name}: ${error.message}`, true)
	}
	currentFile = name + '.html'
	$('file').add(new Option(name, currentFile))
	$('file').value = currentFile
	createEditor(result.html)
	setDirty(true) // nothing is on disk until Save
	showImportReport(result.report)
	say(`Imported ${file.name}. Not saved yet.`)
}

// Shortcuts for the styles that TipTap doesn't already give one (it has Ctrl+Alt+1, 2 and C).
// Ctrl+Alt+0 is TipTap's too, but only for plain paragraphs: ours also unwraps emails and journals.
const StyleShortcuts = Extension.create({
	name: 'styleShortcuts',
	priority: 1000,
	addKeyboardShortcuts() {
		return {
			'Mod-Alt-0': () => (applyStyle('body'), true),
			'Mod-Alt-e': () => (applyStyle('email'), true),
			'Mod-Alt-j': () => (applyStyle('journal'), true),
		}
	},
})

// ---------------------------------------------------------------- the editor

const $ = id => document.getElementById(id)
let editor = null
let currentFile = null
let dirty = false

function createEditor(html) {
	if (editor) editor.destroy()
	// A fresh editor per document, so Undo can never reach back into the previous one.
	editor = new Editor({
		element: $('editor'),
		extensions: [
			StarterKit.configure({
				heading: { levels: [1, 2] },
				blockquote: false,
				bulletList: false,
				orderedList: false,
				listItem: false,
				strike: false,
			}),
			Journal,
			emailField('emailFrom', 'email-from'),
			emailField('emailTo', 'email-to'),
			Email,
			StyleShortcuts,
			Notes,
			SenderColors,
			Figure,
		],
		editorProps: {
			// pictures pasted from the clipboard (a screenshot, say) or dropped from a folder
			handlePaste: (view, event) => {
				const files = pictureFiles(event.clipboardData)
				if (!files.length) return false
				files.forEach(addPicture)
				return true
			},
			handleDrop: (view, event) => {
				const files = pictureFiles(event.dataTransfer)
				if (!files.length) return false
				event.preventDefault()
				files.forEach(addPicture)
				return true
			},
		},
		content: html,
		autofocus: 'start',
		onUpdate: () => setDirty(true),
		onTransaction: () => {
			refreshToolbar()
			refreshProblems()
		},
	})
	setDirty(false)
	refreshToolbar()
	refreshProblems()
}

function refreshToolbar() {
	if (!editor) return
	let current = ''
	let currentItem = null
	for (const button of document.querySelectorAll('[data-block]')) {
		const block = BLOCKS[button.dataset.block]
		const active = block.active(editor)
		button.classList.toggle('active', active)
		if (active) {
			current = block.label
			currentItem = button.closest('#style-menu') ? button : null
		}
	}
	// The style button names the style the cursor is in, drawn in that style's own font.
	const look = currentItem ? getComputedStyle(currentItem) : null
	$('style-current').textContent = currentItem?.firstChild.textContent || current || 'No style'
	Object.assign($('style-current').style, {
		fontFamily: look?.fontFamily ?? '', fontWeight: look?.fontWeight ?? '', fontStyle: look?.fontStyle ?? '',
	})
	for (const button of document.querySelectorAll('[data-mark]')) {
		const mark = button.dataset.mark
		button.classList.toggle('active', editor.isActive(mark))
		button.disabled = !editor.can().toggleMark(mark)
	}
	$('status-block').textContent = current
	const words = editor.getText().trim().split(/\s+/).filter(Boolean).length
	$('status-words').textContent = `${words.toLocaleString()} words`
}

function setDirty(value) {
	dirty = value
	$('status-file').textContent = currentFile ? currentFile + (dirty ? ' (unsaved changes)' : '') : ''
	document.title = (dirty ? '• ' : '') + (currentFile || 'Book Editor')
}

function say(message, isError = false) {
	$('status-message').textContent = message
	$('status-message').classList.toggle('error', isError)
}

// ---------------------------------------------------------------- files

async function api(method, url, body) {
	const response = await fetch(url, { method, body })
	if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
	return response
}

async function refreshFiles() {
	const { files, folder } = await (await api('GET', '/api/files')).json()
	const select = $('file')
	select.replaceChildren(...files.map(name => new Option(name.replace(/\.html$/, ''), name)))
	select.title = `Documents in ${folder}`
	if (currentFile) select.value = currentFile
	return files
}

async function openFile(name) {
	const html = await (await api('GET', `/api/doc/${encodeURIComponent(name)}`)).text()
	showImportReport(null)
	currentFile = name
	$('file').value = name
	createEditor(html)
	remember('lastFile', name)
	say(`Opened ${name}`)
}

async function save() {
	if (!currentFile) return false
	try {
		await api('PUT', `/api/doc/${encodeURIComponent(currentFile)}`, editor.getHTML())
		setDirty(false)
		say(`Saved ${new Date().toLocaleTimeString()}`)
		return true
	} catch (error) {
		say(`Save failed: ${error.message}`, true)
		return false
	}
}

async function exportRtf() {
	if (!(await save())) return
	try {
		const { exported } = await (await api('POST', `/api/export/${encodeURIComponent(currentFile)}`)).json()
		say(`Exported to ${exported}`)
	} catch (error) {
		say(`Export failed: ${error.message}`, true)
	}
}

async function newFile() {
	if (dirty && !confirm('Discard unsaved changes?')) return
	let name = prompt('Name for the new document:')
	if (!name) return
	name = name.trim().replace(/\.html$/i, '') + '.html'
	try {
		await api('PUT', `/api/doc/${encodeURIComponent(name)}?new=1`, '<p></p>')
	} catch (error) {
		return say(`Could not create "${name}": ${error.message}`, true)
	}
	await refreshFiles()
	await openFile(name)
}

// localStorage is only a convenience; the app works without it
function remember(key, value) {
	try { localStorage.setItem(key, value) } catch {}
}
function recall(key) {
	try { return localStorage.getItem(key) } catch { return null }
}

// ---------------------------------------------------------------- wiring

for (const button of document.querySelectorAll('[data-block]')) {
	button.addEventListener('mousedown', event => event.preventDefault()) // keep the text selection
	button.addEventListener('click', () => {
		closeStyleMenu()
		if (button.closest('#style-menu')) applyStyle(button.dataset.block)
		else BLOCKS[button.dataset.block].run(editor)
	})
}

function closeStyleMenu() {
	$('style-menu').hidden = true
	$('style-button').setAttribute('aria-expanded', 'false')
}
$('style-button').addEventListener('mousedown', event => event.preventDefault())
$('style-button').addEventListener('click', () => {
	const opening = $('style-menu').hidden
	$('style-menu').hidden = !opening
	$('style-button').setAttribute('aria-expanded', String(opening))
})
document.addEventListener('mousedown', event => {
	if (!event.target.closest('.style-picker')) closeStyleMenu()
})
document.addEventListener('keydown', event => {
	if (event.key === 'Escape') closeStyleMenu()
})
for (const button of document.querySelectorAll('[data-mark]')) {
	button.addEventListener('mousedown', event => event.preventDefault())
	button.addEventListener('click', () => editor.chain().focus().toggleMark(button.dataset.mark).run())
}

$('picture-file').addEventListener('change', async event => {
	for (const file of event.target.files) await addPicture(file)
	event.target.value = '' // so choosing the same file again still fires
})

$('senders').addEventListener('click', openSenders)
$('sender-add').addEventListener('click', () => {
	const count = document.querySelectorAll('#sender-rows .sender-row').length
	const row = senderRow({ name: '', color: NEW_SENDER_COLORS[count % NEW_SENDER_COLORS.length] })
	$('sender-rows').append(row)
	row.children[1].focus()
})
$('senders-cancel').addEventListener('click', () => $('senders-dialog').close(''))
$('senders-dialog').addEventListener('close', event => {
	if (event.target.returnValue === 'save') saveSenders()
})

$('import').addEventListener('click', () => $('import-file').click())
$('import-file').addEventListener('change', async event => {
	const [file] = event.target.files
	event.target.value = ''
	if (file) await importRtf(file)
})
$('import-report-close').addEventListener('click', () => showImportReport(null))

$('save').addEventListener('click', save)
$('export').addEventListener('click', exportRtf)
$('new').addEventListener('click', newFile)
$('file').addEventListener('change', async event => {
	if (dirty && !confirm('Discard unsaved changes?')) {
		event.target.value = currentFile
		return
	}
	await openFile(event.target.value)
})

function applyZoom(value) {
	document.querySelector('.page').style.zoom = value
	$('zoom').value = value
}
$('zoom').addEventListener('change', event => {
	applyZoom(event.target.value)
	remember('zoom', event.target.value)
})

document.addEventListener('keydown', event => {
	if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
		event.preventDefault()
		save()
	}
})
window.addEventListener('beforeunload', event => {
	if (dirty) event.preventDefault()
})

applyZoom(recall('zoom') || '1.25')
await loadSenders()
const files = await refreshFiles()
if (files.length) {
	const last = recall('lastFile')
	await openFile(files.includes(last) ? last : files[0])
} else {
	say('No documents yet. Click New to start one.')
	createEditor('<p></p>')
}
window.bookEditor = { get editor() { return editor }, addPicture, importRtf } // handy in the browser console
