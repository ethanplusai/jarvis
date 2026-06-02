/**
 * Microphone capture with voice-activity detection (VAD).
 *
 * Replaces the browser Web Speech API (which can't auto-detect language). It
 * records each utterance with MediaRecorder (off the main thread → clean audio,
 * unaffected by the Three.js orb rendering) and uses a lightweight AnalyserNode
 * poll only to decide WHEN an utterance starts and ends. The recorded Opus blob
 * is handed back for the server to transcribe + language-detect with Whisper.
 */

export interface AudioCapture {
  start(): Promise<void>;
  pause(): void;
  resume(): void;
  stop(): void;
}

// Adaptive VAD: thresholds derive from a running ambient-noise estimate, so it
// works whether the mic is loud or quiet without hand-tuned absolute levels.
const START_MULT = 3.5;
const STOP_MULT = 2.0;
const START_FLOOR = 0.006;
const STOP_FLOOR = 0.0035;
const SILENCE_MS = 900; // trailing silence that ends an utterance
const MIN_UTTER_MS = 500; // ignore blips shorter than this
const MAX_UTTER_MS = 18000; // force-flush very long utterances
const POLL_MS = 40;

function rms(buf: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  return Math.sqrt(sum / buf.length);
}

function pickMime(): string {
  const opts = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  for (const m of opts) if (MediaRecorder.isTypeSupported(m)) return m;
  return "";
}

export function createAudioCapture(
  onUtterance: (audio: ArrayBuffer) => void,
  onError: (msg: string) => void
): AudioCapture {
  let ctx: AudioContext | null = null;
  let stream: MediaStream | null = null;
  let analyser: AnalyserNode | null = null;
  let recorder: MediaRecorder | null = null;
  let timer: number | null = null;

  let paused = false;
  let recording = false;
  let noiseFloor = 0.0015;
  let silenceSince = 0;
  let recordStart = 0;
  let parts: Blob[] = [];
  let buf: Float32Array | null = null;
  const mime = pickMime();

  function beginUtterance() {
    if (!recorder || recorder.state !== "inactive") return;
    parts = [];
    recordStart = performance.now();
    silenceSince = 0;
    recording = true;
    recorder.start();
  }

  function endUtterance(keep: boolean) {
    if (!recorder || recorder.state === "inactive") {
      recording = false;
      return;
    }
    recording = false;
    const dur = performance.now() - recordStart;
    (recorder as any)._keep = keep && dur >= MIN_UTTER_MS;
    recorder.stop();
  }

  function poll() {
    if (!analyser || !buf) return;
    if (!paused) {
      analyser.getFloatTimeDomainData(buf as any);
      const level = rms(buf);
      if (!recording) {
        noiseFloor = 0.95 * noiseFloor + 0.05 * level;
        if (level > Math.max(noiseFloor * START_MULT, START_FLOOR)) beginUtterance();
      } else {
        const stopThresh = Math.max(noiseFloor * STOP_MULT, STOP_FLOOR);
        const now = performance.now();
        if (level < stopThresh) {
          if (silenceSince === 0) silenceSince = now;
          else if (now - silenceSince > SILENCE_MS) endUtterance(true);
        } else {
          silenceSince = 0;
        }
        if (now - recordStart > MAX_UTTER_MS) endUtterance(true);
      }
    }
    timer = window.setTimeout(poll, POLL_MS);
  }

  return {
    async start() {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          // Keep Chrome's voice pipeline ON — its auto-gain is what brings the
          // mic up to a usable level (disabling it gave near-silent audio).
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        ctx = new AudioContext();
        const resume = () => { if (ctx && ctx.state === "suspended") ctx.resume(); };
        document.addEventListener("click", resume);
        document.addEventListener("keydown", resume);
        await ctx.resume().catch(() => {});

        // Record the RAW mic stream directly (the standard pattern). The
        // analyser is only a passive level tap for VAD — it never sits in the
        // recording path, so it can't corrupt the audio. The server peak-
        // normalizes, so exact capture level isn't critical.
        const src = ctx.createMediaStreamSource(stream);
        analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        buf = new Float32Array(analyser.fftSize);
        src.connect(analyser);

        recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        recorder.ondataavailable = (e) => { if (e.data && e.data.size > 0) parts.push(e.data); };
        recorder.onstop = () => {
          const keep = (recorder as any)._keep;
          const blob = new Blob(parts, { type: mime || "audio/webm" });
          parts = [];
          if (keep && blob.size > 0) blob.arrayBuffer().then(onUtterance);
        };

        timer = window.setTimeout(poll, POLL_MS);
        console.log("[capture] started; mime=", mime, "ctx.sampleRate=", ctx.sampleRate);
      } catch (e: any) {
        onError(
          e && e.name === "NotAllowedError"
            ? "Microphone access denied. Please allow microphone access."
            : "Could not start the microphone."
        );
      }
    },
    pause() {
      paused = true;
      if (recording) endUtterance(false); // discard whatever was mid-capture
    },
    resume() {
      paused = false;
      silenceSince = 0;
    },
    stop() {
      paused = true;
      if (timer) { clearTimeout(timer); timer = null; }
      try { if (recorder && recorder.state !== "inactive") recorder.stop(); } catch {}
      try { stream?.getTracks().forEach((t) => t.stop()); } catch {}
      try { ctx?.close(); } catch {}
      recorder = null; analyser = null; stream = null; ctx = null; buf = null;
    },
  };
}
