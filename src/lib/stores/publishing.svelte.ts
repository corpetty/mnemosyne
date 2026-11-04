import * as backend from '$lib/api/backend.js';
import { fitExpiry, meetingPeople, mergeQuotes, quoteKey, type Target } from '$lib/app/publishing.js';
import type {
	Job,
	Publication,
	PublicationDefaults,
	PublicationFileResult,
	PublicationListItem,
	PublicationSelection,
	QuoteSpan,
	SessionDetail
} from '$lib/types/index.js';
import { connectionState } from './connection.svelte.js';

/** What the store needs from the backend (and the browser, for saving a file); tests fake it. */
export interface PublishingApi {
	getSettings(): Promise<{
		values: { allow_publishing: boolean; publish_host: string; publish_default_expiry_days: number };
	}>;
	getPublication(sessionId: string): Promise<Publication | null>;
	getPublicationDefaults(sessionId: string): Promise<PublicationDefaults>;
	previewPublication(sessionId: string, selection: PublicationSelection): Promise<unknown>;
	publishSession(sessionId: string, selection: PublicationSelection): Promise<Job>;
	unpublishSession(sessionId: string): Promise<Publication>;
	listPublications(): Promise<PublicationListItem[]>;
	startPublicationFile(sessionId: string, selection: PublicationSelection, password: string | null): Promise<Job>;
	getJob(jobId: string): Promise<Job>;
	/** One image of the meeting's notes as stored (WebP or JPEG), with the connection's headers. */
	fetchNotesImage(sessionId: string, sha256: string): Promise<Blob>;
	/** Save the made file (the backend deletes it after one save or download). False when the
	 * person cancelled the save dialog; nothing returned counts as saved. */
	saveFile(sessionId: string, file: PublicationFileResult): Promise<boolean | void>;
}

/** Save the made file: the desktop app asks where (save dialog) and the backend writes it
 * there, as for a quote's clip; a browser downloads it. A cancelled dialog leaves the file to
 * be pruned by the backend, and nothing is saved. */
async function saveFile(sessionId: string, file: PublicationFileResult): Promise<boolean> {
	const { isTauri } = await import('@tauri-apps/api/core');
	if (isTauri() && connectionState.isLocal) {
		const { save } = await import('@tauri-apps/plugin-dialog');
		const path = await save({
			defaultPath: file.filename || 'meeting.html',
			filters: [{ name: 'HTML', extensions: ['html'] }]
		});
		if (!path) return false;
		await backend.savePublicationFile(sessionId, file.token, path);
		return true;
	}
	await downloadFile(sessionId, file);
	return true;
}

/** Download in one fetch, then hand the bytes to the browser as a file. */
async function downloadFile(sessionId: string, file: PublicationFileResult): Promise<void> {
	const res = await fetch(backend.publicationFileUrl(sessionId, file.token));
	if (!res.ok) throw new Error(`${res.status}: Could not download the file`);
	const blob = await res.blob();
	const a = document.createElement('a');
	a.href = URL.createObjectURL(blob);
	a.download = file.filename || 'meeting.html';
	document.body.appendChild(a);
	a.click();
	a.remove();
	setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
}

/** An image of the notes (`GET /api/sessions/{id}/notes/media/{sha}`). An <img> cannot carry the
 * bearer token, so it is fetched here and shown from an object URL. */
async function fetchNotesImage(sessionId: string, sha256: string): Promise<Blob> {
	const res = await fetch(`${connectionState.url}/api/sessions/${sessionId}/notes/media/${sha256}`, {
		headers: connectionState.headers()
	});
	if (!res.ok) throw new Error(`${res.status}: Could not load the image`);
	return res.blob();
}

const defaultApi: PublishingApi = {
	getSettings: backend.getSettings,
	getPublication: backend.getPublication,
	getPublicationDefaults: backend.getPublicationDefaults,
	previewPublication: backend.previewPublication,
	publishSession: backend.publishSession,
	unpublishSession: backend.unpublishSession,
	listPublications: backend.listPublications,
	startPublicationFile: backend.startPublicationFile,
	getJob: backend.getJob,
	fetchNotesImage,
	saveFile
};

/** "409: The audio is on …" → "The audio is on …". */
export function errorText(e: unknown, fallback: string): string {
	const m = e instanceof Error ? e.message : '';
	return m.replace(/^\d{3}: /, '') || fallback;
}

type Busy = 'publish' | 'unpublish' | 'file' | null;
/** One image of the notes in the dialog's list (generated `NotesImage`). */
export type NotesImage = PublicationDefaults['notes_images'][number];
type MeetingRef = Pick<SessionDetail, 'id' | 'name' | 'attendees' | 'participants'>;

/**
 * Publishing a meeting (backend services/publishing.py): each meeting's live publication, the
 * Publish dialog's choices with a debounced preview, the jobs that publish, update or make a
 * file, quotes picked in the transcript before the dialog opens, and the library's list.
 */
export class PublishingState {
	/** The live publication by meeting id (null: not published; missing: not loaded). */
	publications = $state<Record<string, Publication | null>>({});
	/** Quotes added from the transcript, by meeting, until the dialog takes them. */
	pending = $state<Record<string, QuoteSpan[]>>({});
	list = $state<PublicationListItem[] | null>(null);

	// Settings that shape the dialog (GET /api/settings is readable by everyone).
	allowPublishing = $state(false);
	publishHost = $state('');
	defaultExpiry = $state(90);
	settingsLoaded = $state(false);

	// The dialog
	dialogFor = $state<string | null>(null);
	loading = $state(false);
	selection = $state<PublicationSelection | null>(null);
	defaults = $state<PublicationDefaults | null>(null);
	/** Everyone who could be listed: ticked ones are `selection.attendees`. */
	people = $state<string[]>([]);
	meetingName = $state('');
	target = $state<Target>('local');
	password = $state('');
	preview = $state<unknown>(null);
	previewError = $state('');
	previewing = $state(false);
	busy = $state<Busy>(null);
	stage = $state('');
	error = $state('');
	/** Set once this dialog published: it shows the link. */
	justPublished = $state(false);

	#api: PublishingApi;
	#debounceMs: number;
	#pollMs: number;
	#timer: ReturnType<typeof setTimeout> | null = null;
	#previewSeq = 0;
	#openSeq = 0;
	/** Object URLs of the open dialog's notes images by plain hash, revoked when it closes. */
	#imageUrls = new Map<string, Promise<string>>();

	constructor(api: PublishingApi = defaultApi, debounceMs = 400, pollMs = 500) {
		this.#api = api;
		this.#debounceMs = debounceMs;
		this.#pollMs = pollMs;
	}

	/** Publishing is on: always on the desktop, on a team server once an admin allows it. */
	get allowed(): boolean {
		return !connectionState.me?.team_mode || this.allowPublishing;
	}

	/** The images of the open meeting's notes, in order, each once. */
	get notesImages(): NotesImage[] {
		return this.defaults?.notes_images ?? [];
	}

	/** Whether this image goes into the publication (`images` null = all of them). */
	imageIncluded(sha256: string): boolean {
		const chosen = this.selection?.images;
		return chosen == null || chosen.includes(sha256);
	}

	/** The open meeting's live publication. */
	current = $derived(this.dialogFor ? (this.publications[this.dialogFor] ?? null) : null);

	async loadSettings() {
		try {
			const v = (await this.#api.getSettings()).values;
			this.allowPublishing = !!v.allow_publishing;
			this.publishHost = (v.publish_host ?? '').trim();
			this.defaultExpiry = v.publish_default_expiry_days ?? 90;
			this.settingsLoaded = true;
		} catch {
			/* not connected yet: tried again on the next open */
		}
	}

	/** A meeting's live publication, for the header chip. */
	async load(sessionId: string) {
		try {
			this.publications[sessionId] = await this.#api.getPublication(sessionId);
		} catch {
			/* leave as it was */
		}
	}

	async loadList() {
		try {
			this.list = await this.#api.listPublications();
		} catch {
			this.list = [];
		}
	}

	// ---- quotes picked in the transcript ----------------------------------------------

	/** Add a quote to the meeting's publication-to-be; returns how many it now has. */
	addPending(sessionId: string, span: QuoteSpan): number {
		if (this.dialogFor === sessionId && this.selection) {
			this.setQuotes(mergeQuotes(this.selection.quotes, [span]));
			return this.selection.quotes.length;
		}
		const next = mergeQuotes(this.pending[sessionId] ?? [], [span]);
		this.pending[sessionId] = next;
		const saved = this.publications[sessionId]?.selection.quotes ?? [];
		return mergeQuotes(saved, next).length;
	}

	// ---- the dialog -------------------------------------------------------------------

	async open(session: MeetingRef) {
		const id = session.id;
		const seq = ++this.#openSeq;
		this.#reset();
		this.dialogFor = id;
		this.meetingName = session.name;
		this.loading = true;
		try {
			const [pub, defaults] = await Promise.all([
				this.#api.getPublication(id),
				this.#api.getPublicationDefaults(id),
				this.settingsLoaded ? Promise.resolve() : this.loadSettings()
			]);
			if (seq !== this.#openSeq) return;
			this.publications[id] = pub;
			this.defaults = defaults;
			const { local_reach: _r, local_url: _u, notes_images: _i, ...sel } = defaults;
			const pending = this.pending[id] ?? [];
			delete this.pending[id];
			this.target = sel.where === 'host' && (this.publishHost || defaults.host_url) ? 'host' : 'local';
			this.selection = {
				...sel,
				title: sel.title || session.name,
				quotes: mergeQuotes(sel.quotes ?? [], pending),
				expires_days: fitExpiry(this.target, sel.expires_days, this.defaultExpiry)
			};
			this.people = meetingPeople(session, sel.attendees);
			await this.refreshPreview();
		} catch (e) {
			if (seq === this.#openSeq) this.error = errorText(e, 'Could not open publishing');
		} finally {
			if (seq === this.#openSeq) this.loading = false;
		}
	}

	close() {
		if (this.busy) return;
		this.#openSeq++;
		this.#reset();
	}

	#reset() {
		this.#revokeImages();
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = null;
		this.#previewSeq++;
		this.dialogFor = null;
		this.selection = null;
		this.defaults = null;
		this.people = [];
		this.target = 'local';
		this.password = '';
		this.preview = null;
		this.previewError = '';
		this.previewing = false;
		this.error = '';
		this.stage = '';
		this.justPublished = false;
	}

	/** Change the choices; the preview follows after a pause. */
	set(patch: Partial<PublicationSelection>) {
		if (!this.selection) return;
		this.selection = { ...this.selection, ...patch };
		this.justPublished = false;
		this.schedulePreview();
	}

	setTarget(target: Target) {
		if (!this.selection) return;
		this.target = target;
		this.set({ expires_days: fitExpiry(target, this.selection.expires_days, this.defaultExpiry) });
	}

	toggleAttendee(name: string) {
		if (!this.selection) return;
		const on = this.selection.attendees.includes(name);
		// Kept in the meeting's order, so the published list reads the same every time.
		const chosen = new Set(on ? this.selection.attendees.filter((n) => n !== name) : [...this.selection.attendees, name]);
		this.set({ attendees: this.people.filter((p) => chosen.has(p)) });
	}

	setQuotes(quotes: QuoteSpan[]) {
		this.set({ quotes });
	}

	removeQuote(q: QuoteSpan) {
		if (!this.selection) return;
		const k = quoteKey(q);
		this.setQuotes(this.selection.quotes.filter((x) => quoteKey(x) !== k));
	}

	setClip(q: QuoteSpan, withClip: boolean) {
		if (!this.selection) return;
		const k = quoteKey(q);
		this.setQuotes(this.selection.quotes.map((x) => (quoteKey(x) === k ? { ...x, with_clip: withClip } : x)));
	}

	/** Tick or untick one of the notes' images; `images` is null while all of them are in. */
	setImage(sha256: string, on: boolean) {
		if (!this.selection) return;
		const chosen = new Set(this.notesImages.map((i) => i.sha256).filter((h) => (h === sha256 ? on : this.imageIncluded(h))));
		const all = this.notesImages.every((i) => chosen.has(i.sha256));
		// In the notes' order, so the same choice is always the same request.
		this.set({ images: all ? null : this.notesImages.map((i) => i.sha256).filter((h) => chosen.has(h)) });
	}

	/**
	 * A `blob:` URL for one of the notes' images, for a thumbnail or the preview: fetched once per
	 * dialog with the connection's headers, revoked when the dialog closes.
	 */
	imageUrl(sha256: string): Promise<string> {
		const id = this.dialogFor;
		if (!id) return Promise.reject(new Error('The dialog is closed'));
		let url = this.#imageUrls.get(sha256);
		if (!url) {
			url = this.#api.fetchNotesImage(id, sha256).then((blob) => URL.createObjectURL(blob));
			this.#imageUrls.set(sha256, url);
			// A failure is not kept: opening the list again tries again.
			url.catch(() => this.#imageUrls.get(sha256) === url && this.#imageUrls.delete(sha256));
		}
		return url;
	}

	#revokeImages() {
		for (const url of this.#imageUrls.values()) url.then((u) => URL.revokeObjectURL(u), () => {});
		this.#imageUrls.clear();
	}

	/** The selection as the backend takes it for the chosen target. */
	toSend(): PublicationSelection | null {
		const s = this.selection;
		if (!s) return null;
		return {
			...s,
			title: s.title.trim(),
			where: this.target === 'host' ? 'host' : 'local',
			expires_days: fitExpiry(this.target, s.expires_days, this.defaultExpiry)
		};
	}

	schedulePreview() {
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = setTimeout(() => {
			this.#timer = null;
			void this.refreshPreview();
		}, this.#debounceMs);
	}

	async refreshPreview() {
		const id = this.dialogFor;
		const sel = this.toSend();
		if (!id || !sel) return;
		const seq = ++this.#previewSeq;
		this.previewing = true;
		try {
			const manifest = await this.#api.previewPublication(id, sel);
			if (seq !== this.#previewSeq) return;
			this.preview = manifest;
			this.previewError = '';
		} catch (e) {
			if (seq === this.#previewSeq) this.previewError = errorText(e, 'Could not make the preview');
		} finally {
			if (seq === this.#previewSeq) this.previewing = false;
		}
	}

	/** Wait for a job, showing its stage; throws when it fails. */
	async #follow(job: Job): Promise<Job> {
		let now = job;
		for (;;) {
			if (now.message) this.stage = now.message;
			if (now.status === 'completed') return now;
			if (now.status === 'failed' || now.status === 'cancelled') {
				throw new Error(now.error || (now.status === 'cancelled' ? 'Cancelled' : 'It failed'));
			}
			await new Promise((r) => setTimeout(r, this.#pollMs));
			now = await this.#api.getJob(job.id);
		}
	}

	/** Publish, or update the live publication (same link). */
	async publish(): Promise<Publication | null> {
		const id = this.dialogFor;
		const sel = this.toSend();
		if (!id || !sel || this.busy) return null;
		const updating = !!this.publications[id];
		this.busy = 'publish';
		this.error = '';
		this.stage = '';
		try {
			const done = await this.#follow(await this.#api.publishSession(id, sel));
			const pub = (done.result as { publication?: Publication } | null)?.publication ?? null;
			if (!pub) throw new Error('The backend did not say what it published');
			this.publications[id] = pub;
			// A new link is shown with who can read it; an update keeps the same link.
			if (this.dialogFor === id) this.justPublished = !updating;
			if (this.list) void this.loadList();
			if (this.dialogFor === id) void this.refreshPreview(); // its footer: the next version
			return pub;
		} catch (e) {
			this.error = errorText(e, 'Could not publish');
			return null;
		} finally {
			this.busy = null;
			this.stage = '';
		}
	}

	async unpublish(): Promise<boolean> {
		const id = this.dialogFor;
		if (!id || this.busy) return false;
		this.busy = 'unpublish';
		this.error = '';
		try {
			await this.#api.unpublishSession(id);
			this.publications[id] = null;
			this.justPublished = false;
			if (this.list) this.list = this.list.filter((p) => p.publication.session_id !== id);
			return true;
		} catch (e) {
			this.error = errorText(e, 'Could not unpublish');
			return false;
		} finally {
			this.busy = null;
		}
	}

	/** Make the one-file copy and save it. */
	async saveFile(): Promise<boolean> {
		const id = this.dialogFor;
		const sel = this.toSend();
		if (!id || !sel || this.busy) return false;
		this.busy = 'file';
		this.error = '';
		this.stage = '';
		try {
			const done = await this.#follow(await this.#api.startPublicationFile(id, sel, this.password || null));
			const file = done.result as PublicationFileResult | null;
			if (!file?.token) throw new Error('The backend did not make the file');
			this.stage = 'Saving…';
			return (await this.#api.saveFile(id, file)) !== false;
		} catch (e) {
			this.error = errorText(e, 'Could not make the file');
			return false;
		} finally {
			this.busy = null;
			this.stage = '';
		}
	}
}

export const publishingState = new PublishingState();
