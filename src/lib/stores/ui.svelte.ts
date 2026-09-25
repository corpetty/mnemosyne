/** App-level view state shared by the shell components. */
export type Tab = 'recording' | 'transcript' | 'summary' | 'notes' | 'export';
export type BackendStatus = 'checking' | 'connected' | 'unreachable';
export type SettingsTab = 'general' | 'recording' | 'transcription' | 'ai' | 'notes';
export type ShellStage = 'installing' | 'starting' | 'ready' | 'error' | null;

/** What the main area shows when no meeting is open (an open meeting always wins). */
export type View = 'home' | 'ask' | 'tasks' | 'people' | 'topics' | 'digest' | 'settings' | 'setup';

class UiState {
  view = $state<View>('home');
  settingsTab = $state<SettingsTab>('recording');
  activeTab = $state<Tab>('recording');
  sidebarCollapsed = $state(false);

  backendStatus = $state<BackendStatus>('checking');
  // Progress from the Tauri shell while it installs/starts the backend (release builds).
  shellStage = $state<ShellStage>(null);
  shellMessage = $state('');
  shellLog = $state<string[]>([]);
  /** Background install of the GPU extra (release builds with an NVIDIA driver). */
  gpuInstall = $state<{ state: 'installing' | 'done' | 'error'; message: string } | null>(null);

  closePanels() {
    this.view = 'home';
  }
}

export const uiState = new UiState();
