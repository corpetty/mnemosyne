// The guest notes page's entry: /n/<room>#<key>. The key stays in memory; the URL is left alone
// so a reload still works.
import { mount } from 'svelte';
import App from './App.svelte';
import './guest.css';

mount(App, { target: document.getElementById('app')! });
