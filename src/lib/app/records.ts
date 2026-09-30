import { getJob, recordsExportUrl, startRecordsExport } from '$lib/api/backend.js';

/** A deletion the records period protects asks for a reason (backend services/records.py):
 * ask for it and try again. Returns false when the person gave none. */
export async function withReason(run: (reason: string) => Promise<unknown>): Promise<boolean> {
  try {
    await run('');
    return true;
  } catch (e) {
    const message = e instanceof Error ? e.message : '';
    if (!message.startsWith('400') || !message.includes('give a reason')) throw e;
    const reason = prompt(`${message.replace(/^\d+: /, '')}.\n\nWhy is it deleted? (kept in the deletion log)`);
    if (!reason?.trim()) return false;
    await run(reason.trim());
    return true;
  }
}

/** Export meetings for an exam and download the zip when it is ready. */
export async function exportRecords(body: { session_ids?: string[]; start?: string; end?: string }) {
  const job = await startRecordsExport(body);
  for (;;) {
    await new Promise((r) => setTimeout(r, 1000));
    const now = await getJob(job.id);
    if (now.status === 'completed') {
      const id = (now.result as { export_id?: string } | null)?.export_id;
      if (id) window.location.href = recordsExportUrl(id);
      return (now.result as { meetings?: number } | null)?.meetings ?? 0;
    }
    if (now.status === 'failed' || now.status === 'cancelled') throw new Error(now.error ?? 'The export failed');
  }
}
