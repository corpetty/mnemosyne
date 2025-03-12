import React from 'react';
import FileUpload from '../FileUpload';
import { AudioDevice, UploadMode } from '../types';

interface DeviceSelectionProps {
  devices: AudioDevice[];
  selectedDevices: string[];
  isRecording: boolean;
  isProcessing: boolean;
  uploadMode: UploadMode;
  handleDeviceToggle: (deviceId: string) => void;
  setUploadMode: (mode: UploadMode) => void;
  startRecording: () => Promise<void>;
  stopRecording: () => Promise<void>;
  handleUploadFile: (file: File) => Promise<void>;
  processingStatus: string;
}

const DeviceSelection: React.FC<DeviceSelectionProps> = ({
  devices,
  selectedDevices,
  isRecording,
  isProcessing,
  uploadMode,
  handleDeviceToggle,
  setUploadMode,
  startRecording,
  stopRecording,
  handleUploadFile,
  processingStatus,
}) => {
  return (
    <div className="flex flex-col space-y-4">
      <div className="flex flex-col space-y-2">
        <h3 className="text-sm font-medium text-gray-700">Select Audio Sources:</h3>
        {devices.length > 0 && devices.some(d => d.is_monitor) && (
          <div className="space-y-2">
            <h4 className="text-xs font-medium text-gray-500">System Audio</h4>
            {devices
              .filter(d => d.is_monitor)
              .map((device) => (
                <label key={device.id} className="flex items-center space-x-2">
                  <input
                    type="checkbox"
                    checked={selectedDevices.includes(String(device.id))}
                    onChange={() => handleDeviceToggle(String(device.id))}
                    disabled={isRecording || isProcessing || uploadMode === UploadMode.FileUpload}
                    className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                  />
                  <span className="text-sm text-gray-700">{device.name}</span>
                </label>
              ))}
          </div>
        )}
        <div className="space-y-2">
          <h4 className="text-xs font-medium text-gray-500">Input Devices</h4>
          {devices
            .filter(d => !d.is_monitor)
            .map((device) => (
              <label key={device.id} className="flex items-center space-x-2">
                <input
                  type="checkbox"
                  checked={selectedDevices.includes(String(device.id))}
                  onChange={() => handleDeviceToggle(String(device.id))}
                  disabled={isRecording || isProcessing || uploadMode === UploadMode.FileUpload}
                  className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                />
                <span className="text-sm text-gray-700">
                  {device.name} {device.default ? '(Default)' : ''}
                </span>
              </label>
            ))}
        </div>
      </div>
      <div className="flex flex-col space-y-2">
        {uploadMode === UploadMode.Recording ? (
          <>
            <button
              onClick={isRecording ? stopRecording : startRecording}
              disabled={selectedDevices.length === 0 || isProcessing}
              className={`inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white ${
                selectedDevices.length === 0 || isProcessing
                  ? 'bg-gray-400 cursor-not-allowed'
                  : isRecording
                  ? 'bg-red-600 hover:bg-red-700'
                  : 'bg-green-600 hover:bg-green-700'
              }`}
            >
              {isRecording ? (
                <>
                  <span className="mr-2">⏹</span>
                  Stop Recording
                </>
              ) : (
                <>
                  <span className="mr-2">⏵</span>
                  Start Recording
                </>
              )}
            </button>
            <button
              onClick={() => setUploadMode(UploadMode.FileUpload)}
              disabled={isRecording || isProcessing}
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Upload File
            </button>
          </>
        ) : (
          <>
            <FileUpload onUpload={handleUploadFile} />
            <button
              onClick={() => setUploadMode(UploadMode.Recording)}
              disabled={isProcessing}
              className="inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Record Audio
            </button>
          </>
        )}
        {processingStatus && (
          <div className="text-sm text-gray-600 text-center">
            {processingStatus}
          </div>
        )}
      </div>
    </div>
  );
};

export default DeviceSelection;
