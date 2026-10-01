<script lang="ts">
	import { getMyPrefs, listSummaryStyles, setMyPrefs } from '$lib/api/backend.js';
	import { toastState } from '$lib/stores/toast.svelte.js';
	import type { SummaryStyle, UserPrefs } from '$lib/types/index.js';

	// Your own preferences on a team server (backend services/prefs.py). Blank means the server's
	// setting. Your meetings are summarized, alerted on and pushed to HubSpot with them.
	let p = $state<UserPrefs | null>(null);
	let styles = $state<SummaryStyle[]>([]);
	let saving = $state(false);
	const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

	$effect(() => {
		getMyPrefs()
			.then((v) => (p = v))
			.catch(() => (p = null));
		listSummaryStyles()
			.then((s) => (styles = s))
			.catch(() => (styles = []));
	});

	const blankToNull = (v: string | null | undefined) => (v && v.trim() ? v.trim() : null);

	async function save() {
		if (!p) return;
		saving = true;
		try {
			p = await setMyPrefs({
				...p,
				summary_style: p.summary_style || null,
				summary_instructions: blankToNull(p.summary_instructions),
				mention_keywords: blankToNull(p.mention_keywords),
				calendar_ics_url: blankToNull(p.calendar_ics_url),
				hubspot_owner_email: blankToNull(p.hubspot_owner_email)
			});
			toastState.success('Your preferences are saved');
		} catch (e) {
			toastState.error(e instanceof Error ? e.message : 'Could not save your preferences');
		} finally {
			saving = false;
		}
	}

	const inputClass = 'bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 w-full';
	const labelClass = 'block text-xs text-gray-500 mb-1';
</script>

{#if p}
	<section aria-label="My preferences">
		<h3 class="text-lg font-semibold text-gray-200 mb-1">My preferences</h3>
		<p class="text-xs text-gray-500 mb-3">Yours alone on this server. Blank uses the server's setting.</p>
		<div class="grid grid-cols-2 gap-3">
			<label>
				<span class={labelClass}>Summary style for my meetings</span>
				<select bind:value={p.summary_style} class={inputClass}>
					<option value={null}>The server's</option>
					{#each styles as s (s.id)}<option value={s.id}>{s.id}</option>{/each}
				</select>
			</label>
			<label>
				<span class={labelClass}>Share the meetings I create</span>
				<select bind:value={p.share_new_meetings} class={inputClass}>
					<option value={null}>Only when I share them</option>
					<option value="team">With everyone on the team</option>
				</select>
			</label>
			<label class="col-span-2">
				<span class={labelClass}>Instructions added to my summaries</span>
				<textarea rows="2" bind:value={p.summary_instructions} placeholder="e.g. Always list the customer's open questions." class={inputClass}></textarea>
			</label>
			<label class="col-span-2">
				<span class={labelClass}>Alert me when these are said while I record (comma-separated, e.g. your name)</span>
				<input bind:value={p.mention_keywords} class={inputClass} />
			</label>
			<label class="col-span-2">
				<span class={labelClass}>My calendar (private ICS link): names my recordings, offers invitees, shares with invited teammates</span>
				<input type="url" bind:value={p.calendar_ics_url} placeholder="https://calendar.google.com/calendar/ical/…/basic.ics" class={inputClass} />
			</label>
			<label>
				<span class={labelClass}>My HubSpot email (tasks I send are assigned to me)</span>
				<input type="email" bind:value={p.hubspot_owner_email} class={inputClass} />
			</label>
			<label>
				<span class={labelClass}>My weekly digest</span>
				<span class="flex gap-2">
					<select bind:value={p.digest_weekday} class={inputClass}>
						<option value={null}>The server's</option>
						<option value={-1}>Off</option>
						{#each WEEKDAYS as d, i (d)}<option value={i}>{d}</option>{/each}
					</select>
					<select bind:value={p.digest_hour} class="{inputClass} w-24" aria-label="Digest hour">
						<option value={null}>—</option>
						{#each Array.from({ length: 24 }, (_, h) => h) as h (h)}<option value={h}>{String(h).padStart(2, '0')}:00</option>{/each}
					</select>
				</span>
			</label>
		</div>
		<button onclick={save} disabled={saving} class="mt-3 px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50">
			{saving ? 'Saving…' : 'Save my preferences'}
		</button>
	</section>
{/if}
