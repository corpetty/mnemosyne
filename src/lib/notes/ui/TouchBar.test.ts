// The touch bar in jsdom: stubbed matchMedia and visualViewport.
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import TouchBar from './TouchBar.svelte';

let target: HTMLElement;
let comp: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (comp) unmount(comp);
  comp = null;
  target?.remove();
  delete (window as unknown as Record<string, unknown>).visualViewport;
});

function setup(coarse: boolean, visible = true, readOnly = false) {
  const mql = new EventTarget() as EventTarget & { matches: boolean };
  mql.matches = coarse;
  window.matchMedia = (() => mql) as unknown as typeof window.matchMedia;
  const vv = Object.assign(new EventTarget(), { height: 300, offsetTop: 0 });
  Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true });
  target = document.body.appendChild(document.createElement('div'));
  const noop = () => {};
  comp = mount(TouchBar, {
    target,
    props: {
      active: { kind: null, bold: false, italic: false },
      visible,
      readOnly,
      canComment: true,
      ontoggle: noop,
      onbullet: noop,
      ontask: noop,
      onslash: noop,
      oncomment: noop
    }
  });
  flushSync();
  return { mql, vv };
}

describe('TouchBar', () => {
  it('shows only on coarse pointers', () => {
    setup(false);
    expect(target.querySelector('[role=toolbar]')).toBeNull();
  });

  it('follows the visual viewport and keeps focus on mousedown', () => {
    const { vv } = setup(true);
    const bar = target.querySelector<HTMLElement>('[role=toolbar]')!;
    expect(bar.getAttribute('aria-label')).toBe('Format notes (touch)');
    vv.height = innerHeight - 250;
    vv.dispatchEvent(new Event('resize'));
    flushSync();
    expect(bar.style.bottom).toBe('250px');
    const ev = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    target.querySelector('button')!.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(true);
  });

  it('hides without focus and shows only Comment when read-only', async () => {
    setup(true, false);
    expect(target.querySelector('[role=toolbar]')).toBeNull();
    unmount(comp!);
    target.remove();
    setup(true, true, true);
    await tick();
    const names = [...target.querySelectorAll('button')].map((b) => b.getAttribute('aria-label'));
    expect(names).toEqual(['Comment on the selection']);
  });
});
