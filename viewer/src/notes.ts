// The notes' images in a published meeting. The manifest's notes text keeps an image as a line of
// its own, `![caption](mnemosyne:image/<sha>.<ext>@<w>x<h>)` (backend services/publication_format.py),
// and a left-out one as `[image: caption]`. The app's markdown renderer cannot show the first: its
// sanitizer rightly drops a `mnemosyne:` address. So this pass runs BEFORE the renderer: a line
// whose image the manifest lists becomes an empty placeholder block (`data-mn-image="<slot>"`),
// one without becomes the `[image: caption]` text, and Meeting.svelte fills each placeholder with
// an <img> whose `src` is the decrypted blob URL, set from code. No `mnemosyne:` address, and no
// URL at all, ever goes through the markup.

import type { ImageRef } from './format';

/** One placeholder the pass left in the markdown, by its slot number. */
export interface ImageSlot {
	image: ImageRef;
	caption: string;
}

const IMAGE_LINE = /^!\[((?:[^\]\\\n]|\\.)*)\]\(mnemosyne:image\/([0-9a-f]{64})\.(?:webp|jpeg)@\d+x\d+\)$/;
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const PUNCT = /[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/;

/** Undo markdown's backslash escapes (`\[` is `[`), as the backend does for a caption. */
function unescapeCaption(text: string): string {
	let out = '';
	for (let i = 0; i < text.length; i++) {
		if (text[i] === '\\' && i + 1 < text.length && PUNCT.test(text[i + 1])) {
			out += text[i + 1];
			i++;
		} else out += text[i];
	}
	return out;
}

/** The markdown with its image lines replaced, and the slots of the placeholders in it. */
export function prepareNotes(notes: string, images: readonly ImageRef[]): { markdown: string; slots: ImageSlot[] } {
	const byId = new Map(images.map((i) => [i.id, i]));
	const slots: ImageSlot[] = [];
	const lines = notes.split('\n');
	let fence: string | null = null;
	for (let n = 0; n < lines.length; n++) {
		const line = lines[n];
		const f = FENCE.exec(line);
		if (f) {
			if (fence === null) fence = f[1][0].repeat(f[1].length);
			else if (f[1][0] === fence[0] && f[1].length >= fence.length) fence = null;
			continue;
		}
		if (fence !== null) continue;
		const m = IMAGE_LINE.exec(line);
		if (!m) continue;
		const caption = unescapeCaption(m[1]);
		const image = byId.get(m[2]);
		if (image) {
			slots.push({ image, caption });
			// An HTML block ends at a blank line, so it stands apart from the lines around it.
			lines[n] = `\n<div data-mn-image="${slots.length - 1}"></div>\n`;
		} else {
			lines[n] = caption ? `[image: ${caption}]` : '[image]';
		}
	}
	return { markdown: lines.join('\n'), slots };
}

/** A displayed size: the manifest's number when it is a sensible one. */
function dimension(value: unknown): number | null {
	return typeof value === 'number' && Number.isInteger(value) && value > 0 && value <= 20000 ? value : null;
}

/** The text a picture that is not shown falls back to. */
export function imageText(caption: string): string {
	return caption ? `[image: ${caption}]` : '[image]';
}

/**
 * Fill the placeholders under `root`: each gets its grey box at once, then the picture when
 * `load` gives its blob URL, or the `[image: caption]` text when it cannot. Also turns any other
 * image that lost its address to the sanitizer (an inline `mnemosyne:` one) into that text.
 * Returns a function that stops pending loads from touching the page.
 */
export function fillImages(
	root: HTMLElement,
	slots: readonly ImageSlot[],
	load: ((image: ImageRef) => Promise<string>) | undefined
): () => void {
	let live = true;
	for (const img of Array.from(root.querySelectorAll('img:not([src])'))) {
		img.replaceWith(root.ownerDocument.createTextNode(imageText(img.getAttribute('alt') ?? '')));
	}
	for (const box of Array.from(root.querySelectorAll<HTMLElement>('[data-mn-image]'))) {
		const slot = slots[Number(box.dataset.mnImage)];
		box.replaceChildren();
		if (!slot) {
			box.remove();
			continue;
		}
		const { image, caption } = slot;
		const w = dimension(image.width);
		const h = dimension(image.height);
		box.className = 'mn-image';
		box.setAttribute('role', 'img');
		box.setAttribute('aria-label', caption || 'Image');
		box.setAttribute('aria-busy', 'true');
		if (w && h) {
			box.style.setProperty('width', `${w}px`);
			box.style.setProperty('aspect-ratio', `${w} / ${h}`);
		}
		const fail = () => {
			if (!live) return;
			box.className = 'mn-image-missing';
			box.removeAttribute('role');
			box.removeAttribute('aria-label');
			box.removeAttribute('aria-busy');
			box.removeAttribute('style');
			box.textContent = imageText(caption);
		};
		if (!load) {
			fail();
			continue;
		}
		load(image).then((url) => {
			if (!live) return;
			const img = box.ownerDocument.createElement('img');
			img.alt = caption;
			if (w && h) {
				img.width = w;
				img.height = h;
			}
			img.src = url;
			img.onerror = fail;
			box.className = 'mn-image mn-image-loaded';
			box.removeAttribute('role');
			box.removeAttribute('aria-label');
			box.removeAttribute('aria-busy');
			box.removeAttribute('style');
			box.replaceChildren(img);
		}, fail);
	}
	return () => {
		live = false;
	};
}
