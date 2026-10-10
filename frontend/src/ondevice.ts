// Should recognition run on this machine, or in Google's cloud?
//
// Chrome's SpeechRecognition sends audio to a Google service unless told to
// process locally, and that service goes quiet for minutes at a time: the
// deaf watchdog measured a quiet room, a loud voice and zero transcripts,
// then recovery a few minutes later with nothing changed. From Chrome 139 a
// language pack can be installed once and recognition then never leaves the
// device, which removes that failure and most of the latency with it.
//
// This is the decision, kept pure so it is testable; voice.ts does the
// asking and the installing.

export type Availability = "available" | "downloadable" | "downloading" | "unavailable" | "unsupported";

export interface LocalPlan {
  /** Run the next recogniser with processLocally = true. */
  useLocal: boolean;
  /** Ask the browser to install the language pack first. */
  install: boolean;
  /** One line for the server log, so a machine's answer is on record. */
  say: string;
}

export function planLocalRecognition(status: Availability): LocalPlan {
  switch (status) {
    case "available":
      return { useLocal: true, install: false, say: "on-device recognition: available" };
    case "downloadable":
      return { useLocal: false, install: true, say: "on-device recognition: language pack downloadable; installing" };
    case "downloading":
      return { useLocal: false, install: true, say: "on-device recognition: language pack downloading; waiting" };
    case "unavailable":
      return { useLocal: false, install: false, say: "on-device recognition: unavailable for en-US on this machine; using the cloud" };
    default:
      return { useLocal: false, install: false, say: "on-device recognition: not supported by this browser; using the cloud" };
  }
}

// A local engine that reports one of these is not going to start working by
// being restarted; the cloud is the fallback, not a retry.
const LOCAL_FATAL = new Set(["language-not-supported", "service-not-allowed", "audio-capture-not-supported"]);

export function shouldAbandonLocal(error: string): boolean {
  return LOCAL_FATAL.has(error);
}
