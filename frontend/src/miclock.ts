// One JARVIS tab listens at a time.
//
// Chrome runs exactly one live SpeechRecognition per browser: starting one
// in a second tab ABORTS the first, and an aborted session never delivers
// the sentence it was hearing. Two JARVIS tabs therefore take turns going
// deaf, and neither knows why. Measured live: four voice connections in five
// seconds, then commands lost at random.
//
// So the microphone is a lock (the Web Locks API, shared across tabs of one
// origin). The tab that holds it listens; another tab waits, says so, and
// takes over the moment the holder closes. Written against a small
// interface so the decision is testable without a browser.

export interface LockManagerLike {
  request(name: string, options: { ifAvailable: boolean }, callback: (lock: unknown | null) => Promise<void>): Promise<void>;
}

export const MIC_LOCK = "jarvis-microphone";

export interface MicClaim {
  /** Resolves true when this tab holds the microphone. */
  granted: Promise<boolean>;
  /** Give the microphone back (the tab is closing, or muted for good). */
  release(): void;
}

/**
 * Claim the microphone for this tab. `onWaiting` fires if another tab holds
 * it, and the claim resolves when that tab lets go. With no lock manager
 * (an old browser, a file: page) the claim is granted at once: better one
 * tab that listens than a lock nobody can take.
 */
export function claimMicrophone(locks: LockManagerLike | undefined, onWaiting: () => void): MicClaim {
  if (!locks) {
    return { granted: Promise.resolve(true), release() {} };
  }
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  let settle!: (v: boolean) => void;
  const granted = new Promise<boolean>((resolve) => { settle = resolve; });

  const hold = async (lock: unknown | null) => {
    if (lock === null) {
      onWaiting();
      // Queue for it: this resolves only when the holder releases or closes.
      await locks.request(MIC_LOCK, { ifAvailable: false }, async () => { settle(true); await held; });
      return;
    }
    settle(true);
    await held;
  };
  locks.request(MIC_LOCK, { ifAvailable: true }, hold).catch(() => settle(true));
  return { granted, release };
}
