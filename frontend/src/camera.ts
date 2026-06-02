/**
 * On-demand single-frame webcam capture for JARVIS.
 *
 * Privacy by design: the camera is opened only when the server explicitly
 * requests a frame, one JPEG is captured, and the stream is stopped
 * immediately. There is no continuous feed and nothing is recorded.
 */

/**
 * Capture a single frame from the default webcam and return it as a
 * base64-encoded JPEG (without the `data:` URL prefix), or `null` on failure
 * (no camera, permission denied, etc.).
 */
export async function captureCameraFrame(quality = 0.7): Promise<string | null> {
  let stream: MediaStream | null = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });

    const video = document.createElement("video");
    video.srcObject = stream;
    video.muted = true;
    video.setAttribute("playsinline", "");
    await video.play();

    // Wait until at least one frame has decoded.
    await new Promise<void>((resolve) => {
      if (video.readyState >= 2) {
        resolve();
        return;
      }
      video.onloadeddata = () => resolve();
    });

    // Brief settle so the sensor can auto-expose / focus.
    await new Promise((r) => setTimeout(r, 250));

    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth || 1280;
    canvas.height = video.videoHeight || 720;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    const dataUrl = canvas.toDataURL("image/jpeg", quality);
    return dataUrl.split(",")[1] || null;
  } catch (e) {
    console.error("[camera] capture failed", e);
    return null;
  } finally {
    // Always release the camera — no lingering streams.
    if (stream) stream.getTracks().forEach((t) => t.stop());
  }
}
