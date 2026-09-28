type ToastType = 'info' | 'success' | 'error';

export interface ToastAction {
  label: string;
  run: () => void;
}

interface Toast {
  id: number;
  message: string;
  type: ToastType;
  action?: ToastAction;
}

class ToastState {
  toasts = $state<Toast[]>([]);
  private nextId = 0;

  show(message: string, type: ToastType = 'info', duration = 4000, action?: ToastAction) {
    // The same message again (a retry loop, a burst of events) is shown once.
    const same = this.toasts.find((t) => t.message === message && t.type === type && !t.action);
    if (same && !action) this.dismiss(same.id);
    const id = this.nextId++;
    // At most five: the oldest without an action goes first.
    let kept = this.toasts;
    while (kept.length >= 5) {
      const drop = kept.find((t) => !t.action) ?? kept[0];
      kept = kept.filter((t) => t !== drop);
    }
    this.toasts = [...kept, { id, message, type, action }];
    setTimeout(() => this.dismiss(id), duration);
  }

  dismiss(id: number) {
    this.toasts = this.toasts.filter((t) => t.id !== id);
  }

  success(message: string) {
    this.show(message, 'success');
  }

  error(message: string) {
    this.show(message, 'error', 6000);
  }

  info(message: string) {
    this.show(message, 'info');
  }
}

export const toastState = new ToastState();
