/**
 * WebSocket client for JARVIS server communication.
 */

export type MessageHandler = (msg: Record<string, unknown>) => void;

export interface JarvisSocket {
  send(data: Record<string, unknown>): boolean;
  onState(handler: (connected: boolean) => void): void;
  onMessage(handler: MessageHandler): void;
  /** Runs on every (re)connect, and at once if already connected. */
  onOpen(handler: () => void): void;
  close(): void;
  isConnected(): boolean;
}

export function createSocket(url: string): JarvisSocket {
  let ws: WebSocket | null = null;
  let handlers: MessageHandler[] = [];
  const openHandlers: Array<() => void> = [];
  let reconnectDelay = 1000;
  let closed = false;
  let connected = false;
  const stateHandlers: Array<(connected: boolean) => void> = [];
  function state(value: boolean) {
    connected = value;
    for (const handler of stateHandlers) {
      try { handler(value); } catch (error) { console.warn("[ws] state handler", error); }
    }
  }

  function connect() {
    if (closed) return;

    ws = new WebSocket(url);

    ws.onopen = () => {
      state(true);
      reconnectDelay = 1000;
      console.log("[ws] connected");
      for (const h of openHandlers) { try { h(); } catch (e) { console.warn("[ws] open handler", e); } }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        for (const h of handlers) h(msg);
      } catch {
        console.warn("[ws] bad message", event.data);
      }
    };

    ws.onclose = () => {
      state(false);
      if (!closed) {
        console.log(`[ws] reconnecting in ${reconnectDelay}ms`);
        setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
      }
    };

    ws.onerror = (err) => {
      console.error("[ws] error", err);
      ws?.close();
    };
  }

  connect();

  return {
    send(data) {
      if (ws?.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify(data));
          return true;
        } catch {
          return false;
        }
      }
      return false;
    },
    onState(handler) { stateHandlers.push(handler); handler(connected); },
    onMessage(handler) {
      handlers.push(handler);
    },
    onOpen(handler) {
      // Fires on every (re)connect, and at once if already open: anything
      // the server should hear about this tab is sent from here, not from a
      // moment that may precede the connection.
      openHandlers.push(handler);
      if (connected) handler();
    },
    close() {
      closed = true;
      ws?.close();
    },
    isConnected() {
      return connected;
    },
  };
}
