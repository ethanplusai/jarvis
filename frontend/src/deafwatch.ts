// When is the recogniser DEAF — sound going in, nothing coming out — and
// when is it merely not listening?
//
// The microphone hears JARVIS too. While he speaks, the recogniser is paused
// (or is hearing his own voice, which the server discards as echo), so no
// transcript arrives; the old watchdog counted that as three seconds of
// speech-level sound with no result and restarted the recogniser at the
// very moment the user began their next sentence. Measured live: the first
// command worked, the second was never heard, every time.
//
// So the watch can be HELD: while held, sound is not evidence of anything,
// and when released the clock starts fresh. Pure, clock injected, so it is
// testable without an AudioContext.

export interface DeafWatchOptions {
  deafAfterMs?: number;      // sound with no result for this long is deaf
  complainEveryMs?: number;  // and say so at most this often
  recentLoudMs?: number;     // "sound going in" means loud within this window
}

export class DeafWatch {
  private readonly deafAfterMs: number;
  private readonly complainEveryMs: number;
  private readonly recentLoudMs: number;
  private lastLoudAt = 0;
  private lastResultAt: number;
  private complainedAt = 0;
  private held = false;

  constructor(now: number, opts: DeafWatchOptions = {}) {
    this.deafAfterMs = opts.deafAfterMs ?? 3000;
    this.complainEveryMs = opts.complainEveryMs ?? 15000;
    this.recentLoudMs = opts.recentLoudMs ?? 500;
    this.lastResultAt = now;
  }

  /** Speech-level sound was heard just now. Ignored while held. */
  loud(now: number): void {
    if (!this.held) this.lastLoudAt = now;
  }

  /** The recogniser produced something (a final or an interim). */
  result(now: number): void {
    this.lastResultAt = now;
  }

  /** Stop judging: JARVIS is speaking, or the recogniser is deliberately paused. */
  hold(): void {
    this.held = true;
    this.lastLoudAt = 0;
  }

  /** Judge again, from a clean slate: nothing before `now` counts. */
  release(now: number): void {
    this.held = false;
    this.lastLoudAt = 0;
    this.lastResultAt = now;
  }

  get isHeld(): boolean { return this.held; }

  /** How long the recogniser has returned nothing, for the report. */
  silentFor(now: number): number { return now - this.lastResultAt; }

  /**
   * True when the recogniser should be declared deaf right now: not held,
   * sound going in within the last half second, no result for deafAfterMs,
   * and no complaint within complainEveryMs. Calling it records the complaint.
   */
  check(now: number): boolean {
    if (this.held || !this.lastLoudAt) return false;
    if (now - this.lastLoudAt >= this.recentLoudMs) return false;
    if (now - this.lastResultAt <= this.deafAfterMs) return false;
    if (this.complainedAt && now - this.complainedAt <= this.complainEveryMs) return false;
    this.complainedAt = now;
    return true;
  }
}
