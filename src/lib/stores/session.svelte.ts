import type { SessionSummary, SessionDetail } from '$lib/types/index.js';
import * as api from '$lib/api/backend.js';

function message(e: unknown, fallback: string): string {
	return e instanceof Error ? e.message : fallback;
}

class SessionState {
	sessions = $state<SessionSummary[]>([]);
	activeSession = $state<SessionDetail | null>(null);
	/** True only for the first load: refreshes keep showing the list instead of "Loading...". */
	loading = $state(false);
	creating = $state(false);
	error = $state<string | null>(null);
	/** Set by search: open this session on the transcript tab at this segment. */
	pendingOpen = $state<{ sessionId: string; idx: number | null } | null>(null);
	/** Told about every failure (the controller shows it as a toast). */
	onError: ((message: string) => void) | null = null;

	// The latest open request wins: a slow answer for a meeting the user already left (or an
	// older refresh) never replaces the one on screen.
	#openSeq = 0;
	#refreshing: Promise<void> | null = null;
	#refreshAgain = false;

	#fail(e: unknown, fallback: string) {
		this.error = message(e, fallback);
		this.onError?.(this.error);
	}

	/** Open this meeting (or none) now; a slower open still under way is dropped. */
	show(session: SessionDetail | null) {
		this.#openSeq++;
		this.activeSession = session;
	}

	/** A newer copy of a meeting (an action's answer): taken only if it is still the open one,
	 * so a reply that arrives after the user moved on does not bring the old meeting back. */
	update(session: SessionDetail) {
		if (this.activeSession?.id === session.id) this.activeSession = session;
	}

	async loadSessions() {
		this.loading = this.sessions.length === 0;
		try {
			this.sessions = await api.listSessions();
			this.error = null;
		} catch (e) {
			// Not a toast: this runs on every event, and a backend that is restarting would
			// raise one each time. The list keeps what it had.
			this.error = message(e, 'Could not load the meetings');
		} finally {
			this.loading = false;
		}
	}

	async createSession(name?: string): Promise<SessionDetail | null> {
		if (this.creating) return null; // a double click makes one meeting, not two
		this.creating = true;
		try {
			const session = await api.createSession(name);
			this.show(session);
			await this.loadSessions();
			return session;
		} catch (e) {
			this.#fail(e, 'Could not create a meeting');
			return null;
		} finally {
			this.creating = false;
		}
	}

	async selectSession(sessionId: string) {
		const seq = ++this.#openSeq;
		try {
			const session = await api.getSession(sessionId);
			if (seq === this.#openSeq) this.activeSession = session;
		} catch (e) {
			if (seq === this.#openSeq) this.#fail(e, 'Could not open the meeting');
		}
	}

	async renameSession(sessionId: string, name: string) {
		try {
			this.update(await api.renameSession(sessionId, name));
			await this.loadSessions();
		} catch (e) {
			this.#fail(e, 'Could not rename the meeting');
		}
	}

	async deleteSession(sessionId: string): Promise<boolean> {
		try {
			await api.deleteSession(sessionId);
			if (this.activeSession?.id === sessionId) this.show(null);
			await this.loadSessions();
			return true;
		} catch (e) {
			this.#fail(e, 'Could not delete the meeting');
			return false;
		}
	}

	async updateNotes(sessionId: string, notes: string): Promise<boolean> {
		try {
			this.update(await api.updateNotes(sessionId, notes));
			return true;
		} catch (e) {
			this.#fail(e, 'Could not save the notes');
			return false;
		}
	}

	/** Reload the open meeting. Calls while one is running are folded into one more reload
	 * after it (several events for the same change ask once or twice, not once each). */
	async refreshActive() {
		if (this.#refreshing) {
			this.#refreshAgain = true;
			return this.#refreshing;
		}
		const run = this.#refreshLoop();
		this.#refreshing = run; // cleared only after it is set (the loop can end at once)
		try {
			await run;
		} finally {
			if (this.#refreshing === run) this.#refreshing = null;
		}
	}

	async #refreshLoop() {
		do {
			this.#refreshAgain = false;
			const id = this.activeSession?.id;
			if (!id) return;
			// No new sequence number: a meeting the user is opening meanwhile wins.
			const seq = this.#openSeq;
			try {
				const session = await api.getSession(id);
				if (seq === this.#openSeq) this.update(session);
			} catch {
				/* a refresh that fails keeps what is on screen */
			}
		} while (this.#refreshAgain);
	}
}

export const sessionState = new SessionState();
