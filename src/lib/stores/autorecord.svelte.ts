/** Auto-record state. The behaviour lives in app/controller.svelte.ts (it starts and stops
 * recordings); this only holds what the banner and the controller share. */
export type AutoMode = 'off' | 'ask' | 'auto';

export interface AutoStarted {
  by: 'app' | 'calendar';
  app?: string;
  uid?: string;
  end?: string; // calendar meeting end (ISO)
}

class AutoRecordState {
  mode = $state<AutoMode>('off');
  silenceMinutes = $state(10);
  /** "Zoom is using the microphone. Record?" (ask mode) */
  offer = $state<string | null>(null);
  /** Set when the current recording was started automatically. */
  started = $state<AutoStarted | null>(null);
}

export const autoRecordState = new AutoRecordState();
