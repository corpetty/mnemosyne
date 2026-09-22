/**
 * Playback state for the active session's audio. The <audio> element lives in
 * AudioPlayer.svelte and registers itself here; the transcript asks to seek.
 */
class PlayerState {
  currentTime = $state(0);
  duration = $state(0);
  playing = $state(false);
  available = $state(false);

  private el: HTMLAudioElement | null = null;

  attach(el: HTMLAudioElement | null) {
    this.el = el;
  }

  seek(seconds: number, play = true) {
    if (!this.el) return;
    this.el.currentTime = Math.max(0, seconds);
    if (play) void this.el.play().catch(() => {});
  }

  toggle() {
    if (!this.el) return;
    if (this.el.paused) void this.el.play().catch(() => {});
    else this.el.pause();
  }

  reset() {
    this.currentTime = 0;
    this.duration = 0;
    this.playing = false;
    this.available = false;
  }
}

export const playerState = new PlayerState();
