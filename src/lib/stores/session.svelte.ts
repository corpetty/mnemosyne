import type { SessionSummary, SessionDetail } from '$lib/types/index.js';
import * as api from '$lib/api/backend.js';

class SessionState {
	sessions = $state<SessionSummary[]>([]);
	activeSession = $state<SessionDetail | null>(null);
	loading = $state(false);
	error = $state<string | null>(null);

	async loadSessions() {
		this.loading = true;
		this.error = null;
		try {
			this.sessions = await api.listSessions();
		} catch (e) {
			this.error = e instanceof Error ? e.message : 'Failed to load sessions';
		} finally {
			this.loading = false;
		}
	}

	async createSession(name?: string): Promise<SessionDetail | null> {
		this.error = null;
		try {
			const session = await api.createSession(name);
			this.activeSession = session;
			await this.loadSessions();
			return session;
		} catch (e) {
			this.error = e instanceof Error ? e.message : 'Failed to create session';
			return null;
		}
	}

	async selectSession(sessionId: string) {
		this.error = null;
		try {
			this.activeSession = await api.getSession(sessionId);
		} catch (e) {
			this.error = e instanceof Error ? e.message : 'Failed to load session';
		}
	}

	async renameSession(sessionId: string, name: string) {
		this.error = null;
		try {
			const updated = await api.renameSession(sessionId, name);
			if (this.activeSession?.id === sessionId) {
				this.activeSession = updated;
			}
			await this.loadSessions();
		} catch (e) {
			this.error = e instanceof Error ? e.message : 'Failed to rename session';
		}
	}

	async deleteSession(sessionId: string) {
		this.error = null;
		try {
			await api.deleteSession(sessionId);
			if (this.activeSession?.id === sessionId) {
				this.activeSession = null;
			}
			await this.loadSessions();
		} catch (e) {
			this.error = e instanceof Error ? e.message : 'Failed to delete session';
		}
	}

	async updateNotes(sessionId: string, notes: string) {
		this.error = null;
		try {
			const updated = await api.updateNotes(sessionId, notes);
			if (this.activeSession?.id === sessionId) {
				this.activeSession = updated;
			}
		} catch (e) {
			this.error = e instanceof Error ? e.message : 'Failed to update notes';
		}
	}

	async refreshActive() {
		if (this.activeSession) {
			await this.selectSession(this.activeSession.id);
		}
	}
}

export const sessionState = new SessionState();
