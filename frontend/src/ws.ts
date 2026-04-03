/**
 * WebSocket client for JARVIS server communication.
 */

export type MessageHandler = (msg: Record<string, unknown>) => void;
export type ConnectionHandler = (connected: boolean) => void;

export interface JarvisSocket {
  send(data: Record<string, unknown>): void;
  onMessage(handler: MessageHandler): void;
  onOpen(handler: ConnectionHandler): void;
  onConnectionChange(handler: ConnectionHandler): void;
  close(): void;
  isConnected(): boolean;
}

export function createSocket(url: string): JarvisSocket {
  let ws: WebSocket | null = null;
  let handlers: MessageHandler[] = [];
  let openHandlers: ConnectionHandler[] = [];
  let connectionHandlers: ConnectionHandler[] = [];
  let reconnectDelay = 1000;
  let closed = false;
  let connected = false;

  function connect() {
    if (closed) return;

    ws = new WebSocket(url);

    ws.onopen = () => {
      connected = true;
      reconnectDelay = 1000;
      console.log("[ws] connected");
      for (const h of openHandlers) h(true);
      for (const h of connectionHandlers) h(true);
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
      connected = false;
      for (const h of connectionHandlers) h(false);
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
        ws.send(JSON.stringify(data));
      }
    },
    onMessage(handler) {
      handlers.push(handler);
    },
    onOpen(handler) {
      openHandlers.push(handler);
      if (connected) handler(true);
    },
    onConnectionChange(handler) {
      connectionHandlers.push(handler);
      handler(connected);
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
