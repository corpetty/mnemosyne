// Builds single.html next to index.html: the same bundle with its JS and CSS inlined, a CSP
// <meta> that allows exactly those two (sha256 hashes) and no requests at all, and an empty
// payload slot that the file export fills (see PAYLOAD_SLOT).
import { createHash } from 'node:crypto';
import type { Plugin } from 'vite';

/**
 * The file export replaces `__PAYLOAD__` (it appears exactly once in single.html) with the
 * FilePayload JSON (viewer/src/load.ts). The JSON must not contain `<`: write it with `<`
 * escaped as `<` (base64url and numbers never contain one anyway).
 */
export const PAYLOAD_PLACEHOLDER = '__PAYLOAD__';
export const PAYLOAD_SLOT = `<script type="application/json" id="payload">${PAYLOAD_PLACEHOLDER}</script>`;

function hash(text: string): string {
	return `'sha256-${createHash('sha256').update(text, 'utf8').digest('base64')}'`;
}

export function singleHtml(js: string, css: string): string {
	const csp = [
		"default-src 'none'",
		`script-src ${hash(js)}`,
		`style-src ${hash(css)}`,
		"connect-src 'none'",
		'media-src blob:',
		'img-src data: blob:',
		"base-uri 'none'",
		"form-action 'none'"
	].join('; ');
	return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>Mnemosyne</title>
<link rel="icon" href="data:,">
<style>${css}</style>
</head>
<body>
<div id="app"></div>
${PAYLOAD_SLOT}
<script type="module">${js}</script>
</body>
</html>
`;
}

export function singleFile(): Plugin {
	return {
		name: 'mnemosyne-viewer-single',
		apply: 'build',
		enforce: 'post',
		generateBundle(_options, bundle) {
			const files = Object.values(bundle);
			const chunks = files.filter((f) => f.type === 'chunk');
			if (chunks.length !== 1) {
				this.error(`single.html needs exactly one JS chunk, the build made ${chunks.length}`);
			}
			const js = chunks[0].type === 'chunk' ? chunks[0].code : '';
			const css = files
				.filter((f) => f.type === 'asset' && f.fileName.endsWith('.css'))
				.map((f) => (f.type === 'asset' ? (typeof f.source === 'string' ? f.source : new TextDecoder().decode(f.source)) : ''))
				.join('');
			if (!css) this.error('single.html: the build made no CSS');
			// Inlined text must not end its element early. "<!--" (Svelte's templates have it) is
			// harmless in script data unless a "<script" follows, which would make the parser
			// skip the real "</script>"; so neither "</script" nor "<script" may appear.
			if (/<\/?script[\s/>]/i.test(js)) this.error('single.html: the JS contains "<script" or "</script"');
			if (/<\/style/i.test(css)) this.error('single.html: the CSS contains "</style"');
			if (js.includes(PAYLOAD_PLACEHOLDER) || css.includes(PAYLOAD_PLACEHOLDER)) {
				this.error(`single.html: ${PAYLOAD_PLACEHOLDER} must appear only in the payload slot`);
			}
			this.emitFile({ type: 'asset', fileName: 'single.html', source: singleHtml(js, css) });
		}
	};
}
