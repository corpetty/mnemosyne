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
    const id = this.nextId++;
    this.toasts = [...this.toasts, { id, message, type, action }];
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
