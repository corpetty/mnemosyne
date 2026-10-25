// The desktop's live carets for the notes editor (phase 11, task 12): adapts the meeting's
// NotesDocState (members, guestMembers and their selections, kept from `presence`, `caret`,
// `guests` and `guest_caret`) to the editor's PresenceSource.
import type { NotesDocState } from '$lib/notes/doc.svelte.js';
import type { CaretView, PresenceSource } from '$lib/notes/ui/types.js';

export function createNotesPresence(st: NotesDocState): PresenceSource {
  return {
    setSel: (sel) => st.setSel(sel),
    carets(): CaretView[] {
      const out: CaretView[] = [];
      for (const m of st.members) {
        const sel = st.sels[m.key];
        if (sel) out.push({ key: m.key, name: m.name, who: m.who || m.name, sel });
      }
      for (const g of st.guestMembers) {
        const sel = st.sels[g.key];
        // The guest's id is the colour source, as on the guest page.
        if (sel) out.push({ key: g.key, name: g.name, who: g.key.slice(2), sel });
      }
      return out;
    },
    subscribe: (fn) => st.onPresence(fn)
  };
}
