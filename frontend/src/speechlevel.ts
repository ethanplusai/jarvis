// Is this microphone level someone talking, or the room?
//
// The mic monitor decides the recogniser is DEAF when sound goes in and no
// transcript comes out, and restarts it — which aborts whatever sentence was
// in flight. That verdict used to rest on one fixed number, 0.02 RMS,
// measured on one laptop. On a microphone whose room tone alone reads 0.02
// to 0.04, "sound going in" was true all day, the recogniser was restarted
// every fifteen seconds in silence, and sentences that happened to overlap
// a restart were lost. Measured live: levels of 0.021 to 0.108 reported as
// DEAF with nobody speaking.
//
// So the room is measured rather than assumed. The noise floor is the 20th
// percentile of the last ten seconds of levels; speech has to stand well
// above it, and has to stay there for a few samples in a row, because a
// single loud sample is a click, a cough or a keypress.

export const SPEECH_LEVEL_MIN = 0.02;   // below this is never speech, whatever the room
export const NOISE_MULTIPLIER = 3;      // speech is at least this many times the room tone
export const NOISE_MARGIN = 0.01;       // and at least this far above it
export const SUSTAIN_SAMPLES = 3;       // 600 ms at one sample every 200 ms
export const WINDOW_SAMPLES = 50;       // 10 s of samples defines "the room"

export class SpeechLevelDetector {
  private samples: number[] = [];
  private loudRun = 0;
  /** The room tone, as measured. */
  noiseFloor = 0;
  /** What a level has to exceed to count as speech right now. */
  threshold = SPEECH_LEVEL_MIN;

  /**
   * Feed one RMS level. True when the sound is sustained and well above
   * the room: someone is talking (or the recogniser should be hearing
   * something). False for silence, room tone, and isolated spikes.
   */
  sample(rms: number): boolean {
    this.samples.push(rms);
    if (this.samples.length > WINDOW_SAMPLES) this.samples.shift();
    const sorted = [...this.samples].sort((a, b) => a - b);
    this.noiseFloor = sorted[Math.floor(sorted.length * 0.2)] ?? 0;
    this.threshold = Math.max(
      SPEECH_LEVEL_MIN,
      this.noiseFloor * NOISE_MULTIPLIER,
      this.noiseFloor + NOISE_MARGIN,
    );
    this.loudRun = rms > this.threshold ? this.loudRun + 1 : 0;
    return this.loudRun >= SUSTAIN_SAMPLES;
  }
}
