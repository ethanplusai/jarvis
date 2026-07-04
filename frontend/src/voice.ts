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
  getLanguage(): string;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const webkitSpeechRecognition: any;

const DEFAULT_SPEECH_LANGUAGE = "en-US";
const DUPLICATE_WINDOW_MS = 1200;

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void,
  initialLanguage = localStorage.getItem("jarvis.speechLanguage") || DEFAULT_SPEECH_LANGUAGE
): VoiceInput {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const SR = (window as any).SpeechRecognition || (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null);
  if (!SR) {
    onError("Speech recognition not supported in this browser. Use Chrome/Chromium for live voice capture.");
    return { start() {}, stop() {}, pause() {}, resume() {}, setLanguage() {}, getLanguage() { return initialLanguage; } };
  }

  const recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = initialLanguage;
  recognition.maxAlternatives = 1;

  let shouldListen = false;
  let paused = false;
  let starting = false;
  let lastFinalText = "";
  let lastFinalAt = 0;

  function safeStart() {
    if (!shouldListen || paused || starting) return;
    starting = true;
    window.setTimeout(() => {
      try {
        recognition.start();
      } catch {
        // Already started by the browser runtime.
      } finally {
        starting = false;
      }
    }, 80);
  }

  recognition.onstart = () => {
    starting = false;
  };

  recognition.onresult = (event: any) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        const text = event.results[i][0].transcript.trim().replace(/\s+/g, " ");
        const now = Date.now();
        if (!text) continue;
        if (text.toLowerCase() === lastFinalText.toLowerCase() && now - lastFinalAt < DUPLICATE_WINDOW_MS) {
          continue;
        }
        lastFinalText = text;
        lastFinalAt = now;
        onTranscript(text);
      }
    }
  };

  recognition.onend = () => {
    starting = false;
    safeStart();
  };

  recognition.onerror = (event: any) => {
    starting = false;
    if (event.error === "not-allowed") {
      onError("Microphone access denied. Please allow microphone access and refresh JARVIS.");
      shouldListen = false;
    } else if (event.error === "audio-capture") {
      onError("No microphone detected. Check your input device, sir.");
      shouldListen = false;
    } else if (event.error === "network") {
      onError("Speech recognition network error. Voice capture will retry shortly.");
    } else if (event.error === "no-speech" || event.error === "aborted") {
      // Expected during pauses and quiet rooms.
    } else {
      console.warn("[voice] recognition error:", event.error);
    }
  };

  return {
    start() {
      shouldListen = true;
      paused = false;
      safeStart();
    },
    stop() {
      shouldListen = false;
      paused = false;
      starting = false;
      recognition.stop();
    },
    pause() {
      paused = true;
      starting = false;
      recognition.stop();
    },
    resume() {
      paused = false;
      safeStart();
    },
    setLanguage(lang: string) {
      const next = lang || DEFAULT_SPEECH_LANGUAGE;
      localStorage.setItem("jarvis.speechLanguage", next);
      recognition.lang = next;
      if (shouldListen && !paused) {
        try {
          recognition.stop();
        } catch {
          safeStart();
        }
      }
    },
    getLanguage() {
      return recognition.lang || DEFAULT_SPEECH_LANGUAGE;
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
        // Skip bad audio. Always advance — playNext() fires the finished callback
        // when the queue is empty, so a failed last buffer can't wedge the orb.
        if (!isPlaying) playNext();
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
