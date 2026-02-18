const WS_URL = 'ws://127.0.0.1:8008/ws';

type MessageHandler = (msg: Record<string, unknown>) => void;

class WebSocketState {
	connected = $state(false);
	private ws: WebSocket | null = null;
	private handlers: MessageHandler[] = [];
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

	connect() {
		if (this.ws?.readyState === WebSocket.OPEN) return;

		this.ws = new WebSocket(WS_URL);

		this.ws.onopen = () => {
			this.connected = true;
			if (this.reconnectTimer) {
				clearTimeout(this.reconnectTimer);
				this.reconnectTimer = null;
			}
		};

		this.ws.onclose = () => {
			this.connected = false;
			this.scheduleReconnect();
		};

		this.ws.onerror = () => {
			this.ws?.close();
		};

		this.ws.onmessage = (event) => {
			try {
				const msg = JSON.parse(event.data);
				for (const handler of this.handlers) {
					handler(msg);
				}
			} catch {
				// ignore malformed messages
			}
		};
	}

	disconnect() {
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		this.ws?.close();
		this.ws = null;
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
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			this.connect();
		}, 3000);
	}
}

export const wsState = new WebSocketState();
