/**
 * Voice input (Web Speech API) and audio output (AudioContext) for JARVIS.
 */

// ---------------------------------------------------------------------------
// Speech Recognition
// ---------------------------------------------------------------------------

export interface VoiceInput {
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
  setLanguage(lang: string): void;
}

/** Fallback until the configured language arrives from the server. */
export const DEFAULT_SPEECH_LANG = "en-US";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const webkitSpeechRecognition: any;

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void
): VoiceInput {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const SR = (window as any).SpeechRecognition || (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null);
  if (!SR) {
    onError("Speech recognition not supported in this browser");
    return { start() {}, stop() {}, pause() {}, resume() {}, setLanguage() {} };
  }

  let shouldListen = false;
  let paused = false;
  let currentLang = DEFAULT_SPEECH_LANG;

  /**
   * Build a recognition session.
   *
   * Chrome reads `lang` when the object is constructed and ignores later
   * assignment on a session that has already run, so switching language means
   * building a new object rather than re-tagging this one.
   */
  function buildRecognition(): any {
    const r = new SR();
    r.continuous = true;
    r.interimResults = true;
    r.lang = currentLang;

    r.onresult = (event: any) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (event.results[i].isFinal) {
          const text = event.results[i][0].transcript.trim();
          if (text) onTranscript(text);
        }
      }
    };

    r.onend = () => {
      // A session replaced by a language switch must stay down, or it would
      // race the new one for the microphone and keep the old language alive.
      if (r !== recognition) return;
      if (shouldListen && !paused) {
        try {
          r.start();
        } catch {
          // Already started
        }
      }
    };

    r.onerror = (event: any) => {
      if (event.error === "not-allowed") {
        onError("Microphone access denied. Please allow microphone access.");
        shouldListen = false;
      } else if (event.error === "no-speech") {
        // Normal, just restart
      } else if (event.error === "aborted") {
        // Expected during pause
      } else {
        console.warn("[voice] recognition error:", event.error);
      }
    };

    return r;
  }

  let recognition = buildRecognition();

  return {
    start() {
      shouldListen = true;
      paused = false;
      try {
        recognition.start();
      } catch {
        // Already started
      }
    },
    stop() {
      shouldListen = false;
      paused = false;
      recognition.stop();
    },
    pause() {
      paused = true;
      recognition.stop();
    },
    resume() {
      paused = false;
      if (shouldListen) {
        try {
          recognition.start();
        } catch {
          // Already started
        }
      }
    },
    setLanguage(lang: string) {
      if (!lang || lang === currentLang) return;
      currentLang = lang;

      const previous = recognition;
      recognition = buildRecognition();
      // Retires the old session: its onend now sees itself superseded.
      try {
        previous.stop();
      } catch {
        // Wasn't running.
      }

      if (shouldListen && !paused) {
        // Let the retired session release the microphone before claiming it,
        // otherwise Chrome rejects the new start outright.
        setTimeout(() => {
          if (recognition !== previous && shouldListen && !paused) {
            try {
              recognition.start();
            } catch {
              // Already started
            }
          }
        }, 250);
      }
      console.log("[voice] language set to", lang);
    },
  };
}

// ---------------------------------------------------------------------------
// Audio Player
// ---------------------------------------------------------------------------

export interface AudioPlayer {
  enqueue(base64: string): Promise<void>;
  stop(): void;
  getAnalyser(): AnalyserNode;
  onFinished(cb: () => void): void;
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.8;
  analyser.connect(audioCtx.destination);

  const queue: AudioBuffer[] = [];
  let isPlaying = false;
  let currentSource: AudioBufferSourceNode | null = null;
  let finishedCallback: (() => void) | null = null;

  function playNext() {
    if (queue.length === 0) {
      isPlaying = false;
      currentSource = null;
      finishedCallback?.();
      return;
    }

    isPlaying = true;
    const buffer = queue.shift()!;
    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(analyser);
    currentSource = source;

    source.onended = () => {
      if (currentSource === source) {
        playNext();
      }
    };

    source.start();
  }

  return {
    async enqueue(base64: string) {
      // Resume audio context (browser autoplay policy)
      if (audioCtx.state === "suspended") {
        await audioCtx.resume();
      }

      try {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
          bytes[i] = binary.charCodeAt(i);
        }
        const audioBuffer = await audioCtx.decodeAudioData(bytes.buffer.slice(0));
        queue.push(audioBuffer);
        if (!isPlaying) playNext();
      } catch (err) {
        console.error("[audio] decode error:", err);
        // Skip bad audio, continue
        if (!isPlaying && queue.length > 0) playNext();
      }
    },

    stop() {
      queue.length = 0;
      if (currentSource) {
        try {
          currentSource.stop();
        } catch {
          // Already stopped
        }
        currentSource = null;
      }
      isPlaying = false;
    },

    getAnalyser() {
      return analyser;
    },

    onFinished(cb: () => void) {
      finishedCallback = cb;
    },
  };
}
