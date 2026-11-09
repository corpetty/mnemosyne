import { getConnectedDevices, removeConnectedDevice } from '$lib/api/backend.js';
import type { ConnectedDevice } from '$lib/types/index.js';

/** What is connected to this computer (GET /api/connections/devices): phones, computers, sync peers, contacts. */
class DevicesState {
  list = $state<ConnectedDevice[] | null>(null);
  error = $state('');

  async load() {
    try {
      this.list = await getConnectedDevices();
      this.error = '';
    } catch (e) {
      // keep what we had; a team server answers 403 to non-admins
      if (this.list === null) this.list = [];
      this.error = e instanceof Error ? e.message : 'Could not load the devices';
    }
  }

  async remove(d: ConnectedDevice) {
    await removeConnectedDevice(d.remove);
    await this.load();
  }
}

export const devicesState = new DevicesState();
