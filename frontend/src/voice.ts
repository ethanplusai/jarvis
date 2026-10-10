import { SpeechLevelDetector } from "./speechlevel";
import { DeafWatch } from "./deafwatch";
import { planLocalRecognition, shouldAbandonLocal, type Availability } from "./ondevice";

/**
 * Voice input (Web Speech API) and audio output (AudioContext) for JARVIS.
 *
 * The microphone keeps listening while JARVIS speaks — the server decides what
 * is echo and what is a barge-in. Playback is a strictly ordered queue of
 * chunks; each one is acknowledged to the server when it finishes.
 */

// ---------------------------------------------------------------------------
// Voice Input
// ---------------------------------------------------------------------------

export interface VoiceInput {
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
  /** Tear the recogniser down and build a new one, now. For when it is
   *  provably deaf: capturing audio and returning nothing. */
  restart(reason: string): void;
  /** Ask the browser for on-device recognition (Chrome 139+) and install
   *  its language pack if it is only downloadable. Safe to call again; call
   *  it from a user gesture too, since an install may need one. */
  prepareLocal(): Promise<void>;
}

const INTERIM_THROTTLE_MS = 200;
const RETRY_MAX_MS = 30000;

// How often the watchdog looks, and how long the engine may be stopped while
// we believe we are listening before it is treated as dead. Chrome ends a
// continuous session on its own routinely — `onend` restarts it in
// milliseconds — so the grace period only has to outlast a healthy restart.
const WATCHDOG_EVERY_MS = 2000;
const STALL_AFTER_MS = 4000;

// A session that has started but never attached audio is the failure that
// looks like nothing at all: Chrome reports `listening`, fires no error, and
// simply never hears again. Observed live — `no-speech` -> `end` -> `start`,
// then silence for the rest of the session. `audiostart` normally follows
// `start` within a few hundred milliseconds, so a couple of seconds without
// it means the capture never attached.
const AUDIO_ATTACH_MS = 2500;

// Chrome hands `onend` back before it is ready to start again, and restarting
// inside that callback is what produces the deaf session above. A short pause
// costs nothing audible and lets the engine finish tearing down. It is only
// used for RECOVERY now — the normal path never waits, because it never lets
// the microphone stop in the first place (see ROTATE_AFTER_MS).
const RESTART_DELAY_MS = 250;

// Chrome ends a `continuous` session on its own after roughly eight seconds
// without speech, and every end->start leaves a hole with no microphone in
// it. Measured in this app: ~250ms per cycle, landing after a pause — which
// is exactly when someone starts talking again.
//
// So do not wait to be torn down. A second recogniser is started BEFORE the
// running one is stopped, and the old one is only stopped once the new one
// reports that it has audio. There is always at least one live capture, so
// there is no hole to fall into. Both may transcribe the overlap, which is
// what `recentlySent` is for.
const ROTATE_AFTER_MS = 6000;
const DEDUPE_WINDOW_MS = 2500;

// Chrome allows exactly ONE live SpeechRecognition. Starting a second does
// not overlap with the first — it ABORTS it, and an aborted session never
// emits the final result for whatever was being said. Measured: interims for
// "Jarvis why can't you fucking hear me" arrived in full, rotation fired 17ms
// after the last one, and the sentence was destroyed before it could be
// finalised. So a rotation is only ever safe in silence, and this is how much
// silence it needs.
const QUIET_BEFORE_ROTATE_MS = 1500;

// Whether a level is someone talking is decided against the room's own noise
// floor, not a fixed number: see speechlevel.ts for why the fixed one restarted
// the recogniser every fifteen seconds on a noisier microphone.

// Sound going in with nothing coming out for this long is the recogniser
// failing, not the user being quiet. Judged by DeafWatch (deafwatch.ts),
// which is HELD while JARVIS speaks: his own voice is not evidence.
const DEAF_AFTER_MS = 3000;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const webkitSpeechRecognition: any;

/**
 * Watch the microphone itself, independently of speech recognition.
 *
 * SpeechRecognition tells you what it UNDERSTOOD. It says nothing at all when
 * it is capturing and returning nothing, which is indistinguishable in a log
 * from the user simply not talking — so "he could not hear me" left no trace
 * anywhere and could not be told apart from silence.
 *
 * This holds its own getUserMedia stream open for the life of the page and
 * measures the actual signal. It never stops, it is not a session, and it has
 * no timeout. If the level is up and no transcript follows, the microphone is
 * live and the recogniser is the problem — which is exactly the case that
 * used to be invisible.
 */
export function createMicMonitor(
  onLevel: (level: number) => void,
  onEvent: (event: string) => void
): { sawSpeech(): void; hold(): void; release(): void } {
  const detector = new SpeechLevelDetector();
  const watch = new DeafWatch(Date.now(), { deafAfterMs: DEAF_AFTER_MS });

  navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const buf = new Uint8Array(analyser.fftSize);
    onEvent("mic monitor attached");

    setInterval(() => {
      analyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      onLevel(rms);

      const now = Date.now();
      if (detector.sample(rms)) watch.loud(now);

      // Sound went into the microphone and nothing came back out of the
      // recogniser. This is the report that could not be made before.
      if (watch.check(now)) {
        onEvent(`DEAF: mic is hearing speech-level sound (level ${rms.toFixed(3)}, room ` +
                `${detector.noiseFloor.toFixed(3)}, threshold ${detector.threshold.toFixed(3)}) ` +
                `but the recogniser has returned nothing for ${Math.round(watch.silentFor(now) / 1000)}s`);
      }
    }, 200);
  }).catch((e) => {
    onEvent(`mic monitor could not open the microphone: ${e && e.name}`);
  });

  return {
    sawSpeech() { watch.result(Date.now()); },
    // While JARVIS speaks (or the recogniser is deliberately paused) the
    // microphone hears him, not the user: hold the verdict, and start the
    // clock fresh when he stops.
    hold() { watch.hold(); },
    release() { watch.release(Date.now()); },
  };
}

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onInterim: (text: string) => void,
  onError: (msg: string) => void,
  onMicEvent: (event: string) => void = () => {}
): VoiceInput {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const SR = (window as any).SpeechRecognition || (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null);
  if (!SR) {
    onError("Speech recognition not supported in this browser");
    onMicEvent("no SpeechRecognition in this browser");
    return { start() {}, stop() {}, pause() {}, resume() {}, restart() {}, async prepareLocal() {} };
  }

  let shouldListen = false;
  let paused = false;
  let waitingForRetry = false;
  let retryDelay = 1000;
  let lastInterimAt = 0;
  let pendingInterim: string | null = null;
  let interimFlush: ReturnType<typeof setTimeout> | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let stallNoticeAt = 0;
  // Any sign the user is mid-sentence. A rotation during one loses it.
  let lastHeardAt = 0;
  // Recognise on this machine rather than in Google's cloud, once the
  // browser says it can (see ondevice.ts). Decided by prepareLocal().
  let useLocal = false;
  let localProbe: Promise<void> | null = null;
  let lastProbeAt = 0;

  const LANGS = ["en-US"];
  async function probeLocal(): Promise<void> {
    if (typeof SR.available !== "function") { mark(planLocalRecognition("unsupported").say); return; }
    let status: Availability;
    try { status = await SR.available({ langs: LANGS, processLocally: true }); }
    catch (err) { mark(`on-device recognition: available() failed (${(err as Error).message}); using the cloud`); return; }
    const plan = planLocalRecognition(status);
    mark(plan.say);
    let local = plan.useLocal;
    if (plan.install && typeof SR.install === "function") {
      try {
        local = await SR.install({ langs: LANGS, processLocally: true, quality: "dictation" }) === true;
        mark(local ? "on-device recognition: language pack installed" : "on-device recognition: install refused; using the cloud");
      } catch (err) {
        mark(`on-device recognition: install failed (${(err as Error).message}); using the cloud`);
      }
    }
    if (local && !useLocal) {
      useLocal = true;
      if (shouldListen && !paused && anyoneListening()) api.restart("switching to on-device recognition");
    }
  }

  const mark = (what: string) => {
    console.info(`[voice] ${new Date().toLocaleTimeString()} ${what}`);
    onMicEvent(what);
  };

  // ── the pair ──────────────────────────────────────────────────────────
  // Chrome tears a session down on its own and every end->start leaves a
  // hole. So we run two, and hand over while the outgoing one is still
  // listening: `live` is the one we trust, `spare` is the one warming up.
  // Neither is ever stopped until the other has audio.
  interface Engine {
    sr: any;                         // eslint-disable-line @typescript-eslint/no-explicit-any
    running: boolean;
    audio: boolean;
    startedAt: number;
    stoppedAt: number;
    retired: boolean;                // handed over; its results are stale
    local: boolean;                  // processLocally: on-device, not the cloud
  }

  const engines: Engine[] = [];
  let liveIdx = 0;
  let rotateTimer: ReturnType<typeof setTimeout> | null = null;

  // The overlap means the same words can arrive twice, from both engines.
  const recentlySent: { text: string; at: number }[] = [];

  function isDuplicate(text: string): boolean {
    const now = Date.now();
    while (recentlySent.length && now - recentlySent[0].at > DEDUPE_WINDOW_MS) {
      recentlySent.shift();
    }
    const norm = text.trim().toLowerCase();
    if (recentlySent.some((r) => r.text === norm)) return true;
    recentlySent.push({ text: norm, at: now });
    return false;
  }

  function emitInterim(text: string) {
    lastInterimAt = Date.now();
    pendingInterim = null;
    onInterim(text);
  }

  function dropPendingInterim() {
    pendingInterim = null;
    if (interimFlush) { clearTimeout(interimFlush); interimFlush = null; }
  }

  function cancelRetry() {
    waitingForRetry = false;
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  }

  function anyoneListening(): boolean {
    return engines.some((e) => e.running && !e.retired);
  }

  function makeEngine(): Engine {
    const sr = new SR();
    sr.continuous = true;
    sr.interimResults = true;
    sr.lang = "en-US";
    if (useLocal) sr.processLocally = true;
    const e: Engine = { sr, running: false, audio: false, startedAt: 0, stoppedAt: 0, retired: false, local: useLocal };

    sr.onstart = () => {
      e.running = true;
      e.startedAt = Date.now();
      retryDelay = 1000;
      mark(e.local ? "listening (on-device)" : "listening");
    };
    sr.onaudiostart = () => {
      e.audio = true;
      mark("audio attached");
      // We are live. Whoever we replaced can go.
      retireOthers(e);
    };
    sr.onaudioend = () => { e.audio = false; };
    sr.onend = () => {
      e.running = false;
      e.audio = false;
      e.stoppedAt = Date.now();
      // Only worth saying when it was the one we were relying on.
      if (!e.retired) mark("engine ended");
      // Never restart from inside onend — that is what produced sessions
      // that report "listening" and never hear. The watchdog picks it up.
      if (shouldListen && !paused && !waitingForRetry && !anyoneListening()) {
        setTimeout(() => {
          if (shouldListen && !paused && !waitingForRetry && !anyoneListening()) startSpare();
        }, RESTART_DELAY_MS);
      }
    };

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    sr.onresult = (event: any) => {
      if (e.retired) return;              // its overlap twin is authoritative
      handleResult(event);
    };

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    sr.onerror = (event: any) => handleError(event, e);
    return e;
  }

  function retireOthers(keep: Engine) {
    for (const other of engines) {
      if (other === keep || other.retired || !other.running) continue;
      other.retired = true;
      try {
        other.sr.stop();                  // stop, not abort: let its last result land
      } catch {
        // already stopping
      }
    }
  }

  function startSpare() {
    // Reap anything finished so the array cannot grow without bound.
    for (let i = engines.length - 1; i >= 0; i--) {
      if (!engines[i].running && engines[i].retired) engines.splice(i, 1);
    }
    const e = makeEngine();
    engines.push(e);
    liveIdx = engines.length - 1;
    try {
      e.sr.start();
    } catch {
      // Between states. Nothing else retries this — the watchdog does.
      mark("start() refused; watchdog will retry");
      e.running = false;
    }
  }

  // Hand over BEFORE Chrome decides to. The spare is started while the
  // current one is still capturing, and only once the spare reports audio is
  // the old one stopped, so the microphone is never unattended.
  function scheduleRotate() {
    if (rotateTimer) clearTimeout(rotateTimer);
    rotateTimer = setTimeout(() => {
      const quietFor = Date.now() - lastHeardAt;
      if (!shouldListen || paused || waitingForRetry) {
        scheduleRotate();
        return;
      }
      if (quietFor < QUIET_BEFORE_ROTATE_MS) {
        // He is talking. Rotating now would abort the session and throw the
        // sentence away — the exact failure this was meant to prevent. Wait.
        rotateTimer = setTimeout(() => scheduleRotate(), QUIET_BEFORE_ROTATE_MS - quietFor);
        return;
      }
      mark("rotating (quiet; pre-empting the no-speech timeout)");
      startSpare();
      scheduleRotate();
    }, ROTATE_AFTER_MS);
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function handleResult(event: any) {
    lastHeardAt = Date.now();
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) {
        const text = result[0].transcript.trim();
        dropPendingInterim();
        if (text && !isDuplicate(text)) onTranscript(text);
      } else {
        interim += result[0].transcript;
      }
    }
    interim = interim.trim();
    if (!interim) return;
    const wait = INTERIM_THROTTLE_MS - (Date.now() - lastInterimAt);
    if (wait <= 0) {
      if (interimFlush) { clearTimeout(interimFlush); interimFlush = null; }
      emitInterim(interim);
    } else {
      pendingInterim = interim;
      if (!interimFlush) {
        interimFlush = setTimeout(() => {
          interimFlush = null;
          if (pendingInterim) emitInterim(pendingInterim);
        }, wait);
      }
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function handleError(event: any, e: Engine) {
    if (e.local && shouldAbandonLocal(String(event.error))) {
      // A restart will not fix this one; the cloud is the fallback.
      useLocal = false;
      mark(`on-device recognition failed (${event.error}); back to the cloud`);
      api.restart("on-device recognition failed");
      return;
    }
    if (event.error === "not-allowed") {
      onError("Microphone blocked — retrying. Check this origin's mic permission.");
      waitingForRetry = true;
      const delay = retryDelay;
      retryDelay = Math.min(retryDelay * 2, RETRY_MAX_MS);
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = setTimeout(() => {
        retryTimer = null;
        waitingForRetry = false;
        if (shouldListen && !paused) startSpare();
      }, delay);
    } else if (event.error === "no-speech" || event.error === "aborted") {
      // Routine. With rotation running there is normally a live twin already.
    } else if (event.error === "audio-capture") {
      onError("Another app has taken the microphone — quit it, then reload.");
    } else if (event.error === "network") {
      onError("Speech recognition lost its connection — that sentence was dropped.");
    } else {
      onError(`Speech recognition error: ${event.error}`);
    }
    if (!e.retired) mark(`error: ${event.error}`);
  }

  // ── watchdog ──────────────────────────────────────────────────────────
  // Everything above hangs off an event. This is the only thing that runs
  // when no event fires — which is the shape of every failure seen so far.
  setInterval(() => {
    if (!shouldListen || paused || waitingForRetry) return;

    // Nobody is listening at all.
    if (!anyoneListening()) {
      const idle = Date.now() - Math.max(0, ...engines.map((e) => e.stoppedAt));
      if (idle < STALL_AFTER_MS) return;
      if (Date.now() - stallNoticeAt > 30000) {
        stallNoticeAt = Date.now();
        onError("Microphone stalled — restarting it.");
      }
      mark("watchdog: nothing listening; restarting");
      startSpare();
      return;
    }

    // Started, but capture never attached: reports "listening" for ever and
    // hears nothing. Only the absence of `audiostart` gives it away.
    for (const e of engines) {
      if (e.running && !e.retired && !e.audio && Date.now() - e.startedAt > AUDIO_ATTACH_MS) {
        mark("watchdog: listening but no audio; replacing");
        e.retired = true;
        try {
          e.sr.abort();
        } catch {
          // nothing to abort
        }
        startSpare();
        return;
      }
    }
  }, WATCHDOG_EVERY_MS);

  function stopAll(hard: boolean) {
    if (rotateTimer) { clearTimeout(rotateTimer); rotateTimer = null; }
    for (const e of engines) {
      e.retired = true;
      try {
        if (hard) e.sr.abort(); else e.sr.stop();
      } catch {
        // not running
      }
    }
  }

  const api: VoiceInput = {
    prepareLocal() {
      // Once local is on, there is nothing to ask. Until then every call is
      // a fresh attempt (throttled): the first runs at start() with no user
      // gesture, and an install the browser refused there may be allowed
      // from the click that follows.
      if (useLocal) return Promise.resolve();
      if (localProbe) return localProbe;
      if (Date.now() - lastProbeAt < 5000) return Promise.resolve();
      lastProbeAt = Date.now();
      localProbe = probeLocal()
        .catch((err) => mark(`on-device recognition: ${(err as Error).message}`))
        .finally(() => { localProbe = null; });
      return localProbe;
    },
    start() {
      shouldListen = true;
      paused = false;
      cancelRetry();
      api.prepareLocal();
      if (!anyoneListening()) startSpare();
      scheduleRotate();
    },
    stop() {
      shouldListen = false;
      paused = false;
      cancelRetry();
      dropPendingInterim();
      stopAll(false);
    },
    pause() {
      paused = true;
      dropPendingInterim();
      stopAll(false);
    },
    resume() {
      paused = false;
      // Do not cancel a pending retry: the server's status frames call resume()
      // constantly, and that would turn the backoff into a hammer.
      if (shouldListen && !waitingForRetry) {
        if (!anyoneListening()) startSpare();
        scheduleRotate();
      }
    },
    restart(reason: string) {
      if (!shouldListen || paused) return;
      mark(`restarting recogniser: ${reason}`);
      // abort, not stop: a deaf session has nothing worth waiting for, and
      // stop() on one can hang instead of ending.
      stopAll(true);
      lastHeardAt = Date.now();          // do not immediately rotate the new one
      setTimeout(() => {
        if (shouldListen && !paused) startSpare();
      }, RESTART_DELAY_MS);
    },
  };
  return api;
}

// ---------------------------------------------------------------------------
// Audio Player — ordered chunks with acknowledgements
// ---------------------------------------------------------------------------

export interface AudioPlayer {
  enqueue(base64: string, utt: number, idx: number): Promise<void>;
  onNeedsGesture(cb: () => void): void;
  stop(): void;
  dropQueued(): void;
  getAnalyser(): AnalyserNode;
  onPlayed(cb: (utt: number, idx: number) => void): void;
  onFinished(cb: () => void): void;
}

interface QueuedChunk {
  utt: number;
  idx: number;
  buffer: AudioBuffer | null; // null until decoded
  failed: boolean;
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.8;
  analyser.connect(audioCtx.destination);

  const queue: QueuedChunk[] = [];
  let isPlaying = false;
  let currentSource: AudioBufferSourceNode | null = null;
  let playedCallback: ((utt: number, idx: number) => void) | null = null;
  let finishedCallback: (() => void) | null = null;
  let needsGestureCallback: (() => void) | null = null;
  let gestureNoticeShown = false;

  async function ensureRunning(): Promise<void> {
    if (audioCtx.state === "running") return;
    // resume() never rejects without a user gesture in Chrome — it just stays
    // pending. Wait a moment, then tell the user, then keep waiting: the
    // promise resolves on their first click and playback continues from here.
    const resumed = audioCtx.resume();
    const timeout = new Promise<void>((resolve) => setTimeout(resolve, 1500));
    await Promise.race([resumed, timeout]);
    const state: string = audioCtx.state; // TS narrows the early return away; re-read it
    if (state !== "running") {
      if (!gestureNoticeShown) {
        gestureNoticeShown = true;
        needsGestureCallback?.();
      }
      await resumed;
    }
  }

  function playNext() {
    // Skip chunks that failed to decode, but still acknowledge them so the
    // server's ordering advances.
    while (queue.length > 0 && queue[0].failed) {
      const dead = queue.shift()!;
      playedCallback?.(dead.utt, dead.idx);
    }
    if (queue.length === 0) {
      isPlaying = false;
      currentSource = null;
      finishedCallback?.();
      return;
    }
    const head = queue[0];
    if (!head.buffer) {
      // Not decoded yet: enqueue() will call playNext() when it is.
      isPlaying = false;
      currentSource = null;
      return;
    }
    queue.shift();
    isPlaying = true;
    const source = audioCtx.createBufferSource();
    source.buffer = head.buffer;
    source.connect(analyser);
    currentSource = source;
    source.onended = () => {
      if (currentSource === source) {
        currentSource = null;
        playedCallback?.(head.utt, head.idx);
        playNext();
      }
    };
    source.start();
  }

  return {
    async enqueue(base64: string, utt: number, idx: number) {
      // Reserve the slot BEFORE any await, so order is preserved even if a later
      // chunk's decode (or the context resume) completes first.
      const item: QueuedChunk = { utt, idx, buffer: null, failed: false };
      queue.push(item);
      try {
        await ensureRunning();
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        item.buffer = await audioCtx.decodeAudioData(bytes.buffer.slice(0));
      } catch (err) {
        console.error("[audio] chunk unplayable:", err);
        item.failed = true; // still acked in order, so the server's sequence advances
      }
      if (!isPlaying) playNext();
    },

    stop() {
      queue.length = 0;
      if (currentSource) {
        const src = currentSource;
        currentSource = null; // so onended does not ack or chain
        try {
          src.stop();
        } catch {
          // Already stopped
        }
      }
      isPlaying = false;
    },

    dropQueued() {
      // The server keeps exactly one chunk alive: the one playing, or — if
      // nothing has started yet — the head it is still expecting an ack for.
      queue.length = currentSource ? 0 : Math.min(queue.length, 1);
    },

    getAnalyser() {
      return analyser;
    },

    onPlayed(cb) {
      playedCallback = cb;
    },

    onFinished(cb) {
      finishedCallback = cb;
    },

    onNeedsGesture(cb) {
      needsGestureCallback = cb;
    },
  };
}
