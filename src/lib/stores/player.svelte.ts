/**
 * Playback state for the active session's audio. The <audio> element lives in
 * AudioPlayer.svelte and registers itself here; the transcript asks to seek.
 */
class PlayerState {
  currentTime = $state(0);
  duration = $state(0);
  playing = $state(false);
  /** The session has audio that can be played. */
  available = $state(false);
  /**
   * The <audio> element exists. It is created on the first play or seek, not when a meeting
   * opens: in the AppImage a broken media stack made WebKit's web process crash (white
   * window) as soon as an <audio> element loaded metadata.
   */
  wanted = $state(false);

  private el: HTMLAudioElement | null = null;
  private pending: { seek: number | null; play: boolean } | null = null;

  attach(el: HTMLAudioElement | null) {
    this.el = el;
  }

  /** Called by AudioPlayer once metadata is loaded: apply the first request. */
  ready() {
    const p = this.pending;
    this.pending = null;
    if (!p || !this.el) return;
    if (p.seek !== null) this.el.currentTime = Math.max(0, p.seek);
    if (p.play) void this.el.play().catch(() => {});
  }

  seek(seconds: number, play = true) {
    if (!this.el) {
      this.pending = { seek: seconds, play };
      this.wanted = true;
      return;
    }
    this.el.currentTime = Math.max(0, seconds);
    if (play) void this.el.play().catch(() => {});
  }

  toggle() {
    if (!this.el) {
      this.pending = { seek: null, play: true };
      this.wanted = true;
      return;
    }
    if (this.el.paused) void this.el.play().catch(() => {});
    else this.el.pause();
  }

  reset() {
    this.currentTime = 0;
    this.duration = 0;
    this.playing = false;
    this.available = false;
    this.wanted = false;
    this.pending = null;
  }
}

export const playerState = new PlayerState();
