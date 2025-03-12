import { useState, useCallback, useEffect } from 'react';
import { AudioDevice } from '../types';

interface UseDevicesResult {
  devices: AudioDevice[];
  fetchDevices: (forceRefresh?: boolean) => Promise<void>;
  isLoading: boolean;
}

export const useDevices = (setSelectedDevices: (devices: string[]) => void): UseDevicesResult => {
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [hasFetchedDevices, setHasFetchedDevices] = useState<boolean>(false);

  const fetchDevices = useCallback(async (forceRefresh: boolean = false) => {
    if (!forceRefresh && hasFetchedDevices) {
      return;
    }

    setIsLoading(true);
    try {
      const response = await fetch(`http://localhost:8000/devices${forceRefresh ? '?force_refresh=true' : ''}`);
      const data = await response.json();
      console.log('Available devices:', data.devices);
      setDevices(data.devices);

      // First try to find a monitor source
      const monitorDevice = data.devices.find((d: AudioDevice) => d.is_monitor);
      if (monitorDevice) {
        console.log('Found monitor device:', monitorDevice);
        setSelectedDevices([String(monitorDevice.id)]);
      } else {
        // Fall back to default input device
        const defaultDevice = data.devices.find((d: AudioDevice) => d.default);
        if (defaultDevice) {
          console.log('Found default device:', defaultDevice);
          setSelectedDevices([String(defaultDevice.id)]);
        }
      }
      setHasFetchedDevices(true);
    } catch (error) {
      console.error('Error fetching devices:', error);
    } finally {
      setIsLoading(false);
    }
  }, [setSelectedDevices, hasFetchedDevices]);

  useEffect(() => {
    fetchDevices();
  }, [fetchDevices]);

  return { devices, fetchDevices, isLoading };
};
