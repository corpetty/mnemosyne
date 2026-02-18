type ToastType = 'info' | 'success' | 'error';

interface Toast {
  id: number;
  message: string;
  type: ToastType;
}

class ToastState {
  toasts = $state<Toast[]>([]);
  private nextId = 0;

  show(message: string, type: ToastType = 'info', duration = 4000) {
    const id = this.nextId++;
    this.toasts = [...this.toasts, { id, message, type }];
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
