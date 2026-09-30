// Print a Markdown document to PDF with the Chromium Playwright already has (for the pilot's
// papers: docs/security-overview.md, docs/pilot-checklist.md).
//   node scripts/docs-to-pdf.mjs docs/security-overview.md [out.pdf]
import { readFileSync } from 'node:fs';
import { marked } from 'marked';
import { chromium } from '@playwright/test';

const [src, out = src.replace(/\.md$/, '.pdf')] = process.argv.slice(2);
if (!src) throw new Error('usage: node scripts/docs-to-pdf.mjs <file.md> [out.pdf]');
const body = marked.parse(readFileSync(src, 'utf8'));
const html = `<!doctype html><html><head><meta charset="utf-8"><style>
  body { font: 10.5pt/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #111; }
  h1 { font-size: 17pt; margin: 0 0 8pt; } h2 { font-size: 12.5pt; margin: 14pt 0 4pt; }
  table { border-collapse: collapse; width: 100%; font-size: 9.5pt; margin: 6pt 0; }
  th, td { border: 1px solid #bbb; padding: 3pt 5pt; text-align: left; vertical-align: top; }
  code { font-size: 9.5pt; } li { margin: 2pt 0; } ul { padding-left: 16pt; }
</style></head><body>${body}</body></html>`;
const browser = await chromium.launch();
const page = await browser.newPage();
await page.setContent(html);
await page.pdf({ path: out, format: 'Letter', margin: { top: '0.7in', bottom: '0.7in', left: '0.8in', right: '0.8in' } });
await browser.close();
console.log(`Wrote ${out}`);
