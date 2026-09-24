/** App-level view state shared by the shell components. */
export type Tab = 'recording' | 'transcript' | 'summary' | 'notes' | 'export';
export type BackendStatus = 'checking' | 'connected' | 'unreachable';
export type ShellStage = 'installing' | 'starting' | 'ready' | 'error' | null;

class UiState {
  /** Shown when no session is open (a session always takes the main area). */
  showSettings = $state(false);
  showAsk = $state(false);
  showDigest = $state(false);
  showTasks = $state(false);
  activeTab = $state<Tab>('recording');
  sidebarCollapsed = $state(false);

  backendStatus = $state<BackendStatus>('checking');
  // Progress from the Tauri shell while it installs/starts the backend (release builds).
  shellStage = $state<ShellStage>(null);
  shellMessage = $state('');
  shellLog = $state<string[]>([]);

  closePanels() {
    this.showAsk = false;
    this.showDigest = false;
    this.showTasks = false;
    this.showSettings = false;
  }
}

export const uiState = new UiState();
