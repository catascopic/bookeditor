import { Editor, Node } from '@tiptap/core'
import { StarterKit } from '@tiptap/starter-kit'
import { TextSelection } from '@tiptap/pm/state'

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
			return { Enter: next, Tab: next }
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

// A picture with a caption. The caption is the node's text; the picture is an attribute.
const Figure = Node.create({
	name: 'figure',
	group: 'block',
	content: 'inline*',
	defining: true,
	isolating: true,
	draggable: true,
	addAttributes() {
		return { src: { default: null } }
	},
	parseHTML: () => [{
		tag: 'figure',
		contentElement: 'figcaption',
		getAttrs: element => ({ src: element.querySelector('img')?.getAttribute('src') }),
	}],
	renderHTML: ({ node }) => ['figure', ['img', { src: node.attrs.src }], ['figcaption', 0]],
	addNodeView() {
		return ({ node }) => {
			const dom = document.createElement('figure')
			const image = document.createElement('img')
			image.src = node.attrs.src
			image.contentEditable = 'false'
			image.draggable = false
			const caption = document.createElement('figcaption')
			dom.append(image, caption)
			return { dom, contentDOM: caption }
		}
	},
	addKeyboardShortcuts() {
		const inCaption = () => this.editor.state.selection.$from.parent.type.name === 'figure'
		return {
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
	const paragraphs = []
	for (let i = range.startIndex; i < range.endIndex; i++) {
		const child = range.parent.child(i)
		if (child.type.name !== 'paragraph') return false
		paragraphs.push(child)
	}
	const { email, emailFrom, emailTo } = state.schema.nodes
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
		onTransaction: refreshToolbar,
	})
	setDirty(false)
	refreshToolbar()
}

function refreshToolbar() {
	if (!editor) return
	let current = ''
	for (const button of document.querySelectorAll('[data-block]')) {
		const block = BLOCKS[button.dataset.block]
		const active = block.active(editor)
		button.classList.toggle('active', active)
		if (active) current = block.label
	}
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
	button.addEventListener('click', () => BLOCKS[button.dataset.block].run(editor))
}
for (const button of document.querySelectorAll('[data-mark]')) {
	button.addEventListener('mousedown', event => event.preventDefault())
	button.addEventListener('click', () => editor.chain().focus().toggleMark(button.dataset.mark).run())
}

$('picture-file').addEventListener('change', async event => {
	for (const file of event.target.files) await addPicture(file)
	event.target.value = '' // so choosing the same file again still fires
})

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
const files = await refreshFiles()
if (files.length) {
	const last = recall('lastFile')
	await openFile(files.includes(last) ? last : files[0])
} else {
	say('No documents yet. Click New to start one.')
	createEditor('<p></p>')
}
window.bookEditor = { get editor() { return editor }, addPicture } // handy in the browser console
