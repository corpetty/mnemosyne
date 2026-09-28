import { connectionState } from './connection.svelte.js';

type MessageHandler = (msg: Record<string, unknown>) => void;

class WebSocketState {
	connected = $state(false);
	private ws: WebSocket | null = null;
	private handlers: MessageHandler[] = [];
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	private wanted = false;
	private retries = 0;

	connect() {
		const state = this.ws?.readyState;
		if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) return;
		this.wanted = true;
		const ws = new WebSocket(connectionState.wsUrl);
		this.ws = ws;
		// Every handler checks it is still the current socket: a replaced or closed one's late
		// events must not flip `connected` or schedule a second reconnect.
		ws.onopen = () => {
			if (ws !== this.ws) return;
			this.connected = true;
			this.retries = 0;
			if (this.reconnectTimer) {
				clearTimeout(this.reconnectTimer);
				this.reconnectTimer = null;
			}
		};

		ws.onclose = () => {
			if (ws !== this.ws) return;
			this.connected = false;
			this.ws = null;
			if (this.wanted) this.scheduleReconnect();
		};

		ws.onerror = () => ws.close();

		ws.onmessage = (event) => {
			if (ws !== this.ws) return;
			let msg: Record<string, unknown>;
			try {
				msg = JSON.parse(event.data);
			} catch {
				return; // ignore malformed messages
			}
			for (const handler of this.handlers) {
				try {
					handler(msg);
				} catch (e) {
					// One failing handler must not keep the others from the event.
					console.error('WebSocket handler failed', e);
				}
			}
		};
	}

	/** Close and stay closed (until connect() is called again). */
	disconnect() {
		this.wanted = false;
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		const ws = this.ws;
		this.ws = null;
		ws?.close();
		this.connected = false;
	}

	send(msg: Record<string, unknown>) {
		if (this.ws?.readyState === WebSocket.OPEN) {
			this.ws.send(JSON.stringify(msg));
		}
	}

	onMessage(handler: MessageHandler) {
		this.handlers.push(handler);
		return () => {
			this.handlers = this.handlers.filter((h) => h !== handler);
		};
	}

	private scheduleReconnect() {
		if (this.reconnectTimer) return;
		// 1 s, 2 s, 4 s, then every 5 s: back quickly after a blip, not hammering a backend
		// that is still starting.
		const delay = Math.min(1000 * 2 ** this.retries, 5000);
		this.retries++;
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			if (this.wanted) this.connect();
		}, delay);
	}
}

export const wsState = new WebSocketState();
