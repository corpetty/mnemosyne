// The publication viewer's entry. One bundle serves two pages: index.html (hosted at
// /p/<id>#<key>, reads from this origin) and single.html (a file with the publication inlined
// in <script type="application/json" id="payload">, makes no requests at all).
import { mount } from 'svelte';
import type { Source } from './load';
import Viewer from './Viewer.svelte';
import './viewer.css';

const slot = document.getElementById('payload');
const source: Source = slot ? { kind: 'file', payload: slot.textContent } : { kind: 'hosted' };

mount(Viewer, { target: document.getElementById('app')!, props: { source } });
