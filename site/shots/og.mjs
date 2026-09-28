// The link-preview image (site/assets/og.png, 1200x630): the top of the landing page itself.
import { chromium } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const page = fileURLToPath(new URL('../index.html', import.meta.url));
const browser = await chromium.launch();
const tab = await browser.newPage({ viewport: { width: 1200, height: 630 }, colorScheme: 'dark' });
await tab.goto(`file://${page}`);
await tab.evaluate(() => document.fonts.ready);
await tab.addStyleTag({ content: '.nav, .fine, .eyebrow { display: none !important } header.hero { padding-top: 64px }' });
await tab.waitForTimeout(1500);
await tab.screenshot({ path: fileURLToPath(new URL('../assets/og.png', import.meta.url)) });
await browser.close();
console.log('og.png written');
