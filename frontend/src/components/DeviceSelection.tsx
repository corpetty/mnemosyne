import React from 'react';
import FileUpload from '../FileUpload';
import { AudioDevice, UploadMode, Model, TranscriptFile } from '../types';

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
  models: Model[];
  selectedModel: string;
  setSelectedModel: React.Dispatch<React.SetStateAction<string>>;
  transcriptFiles: TranscriptFile[];
  selectedTranscriptFile: string;
  setSelectedTranscriptFile: React.Dispatch<React.SetStateAction<string>>;
  handleResummarize: () => Promise<void>;
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
  models,
  selectedModel,
  setSelectedModel,
  transcriptFiles,
  selectedTranscriptFile,
  setSelectedTranscriptFile,
  handleResummarize,
}) => {
  // Helper function to render model selection dropdown
  const renderModelSelection = () => (
    <div className="mt-4">
      <label htmlFor="model-select" className="block text-sm font-medium text-gray-700">
        Select Model for Summarization:
      </label>
      <select
        id="model-select"
        value={selectedModel}
        onChange={(e) => setSelectedModel(e.target.value)}
        disabled={isRecording || isProcessing}
        className="mt-1 block w-full pl-3 pr-10 py-2 text-base border-gray-300 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm rounded-md"
      >
        <option value="">Default Model</option>
        {models.map((model) => (
          <option key={model.id} value={model.id}>
            {model.name} ({model.provider})
          </option>
        ))}
      </select>
    </div>
  );
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
        {/* Mode selector tabs */}
        <div className="border-b border-gray-200">
          <nav className="-mb-px flex space-x-4" aria-label="Tabs">
            <button
              onClick={() => setUploadMode(UploadMode.Recording)}
              className={`${
                uploadMode === UploadMode.Recording
                  ? 'border-indigo-500 text-indigo-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
              } whitespace-nowrap py-2 px-1 border-b-2 font-medium text-sm`}
              disabled={isRecording || isProcessing}
            >
              Record Audio
            </button>
            <button
              onClick={() => setUploadMode(UploadMode.FileUpload)}
              className={`${
                uploadMode === UploadMode.FileUpload
                  ? 'border-indigo-500 text-indigo-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
              } whitespace-nowrap py-2 px-1 border-b-2 font-medium text-sm`}
              disabled={isRecording || isProcessing}
            >
              Upload File
            </button>
            <button
              onClick={() => setUploadMode(UploadMode.Resummarize)}
              className={`${
                uploadMode === UploadMode.Resummarize
                  ? 'border-indigo-500 text-indigo-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
              } whitespace-nowrap py-2 px-1 border-b-2 font-medium text-sm`}
              disabled={isRecording || isProcessing}
            >
              Re-summarize
            </button>
          </nav>
        </div>

        {uploadMode === UploadMode.Recording ? (
          <>
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
              
              {renderModelSelection()}
            </>
          </>
        ) : uploadMode === UploadMode.FileUpload ? (
          <>
            <FileUpload onUpload={handleUploadFile} />
            {renderModelSelection()}
          </>
        ) : (
          // Resummarize mode
          <>
            <div className="mt-2">
              <label htmlFor="transcript-select" className="block text-sm font-medium text-gray-700">
                Select Transcript to Re-summarize:
              </label>
              <select
                id="transcript-select"
                value={selectedTranscriptFile}
                onChange={(e) => setSelectedTranscriptFile(e.target.value)}
                disabled={isProcessing}
                className="mt-1 block w-full pl-3 pr-10 py-2 text-base border-gray-300 focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm rounded-md"
              >
                <option value="">Select a transcript</option>
                {transcriptFiles.map((file) => (
                  <option key={file.path} value={file.path}>
                    {file.filename} ({file.date})
                  </option>
                ))}
              </select>
            </div>
            
            {renderModelSelection()}
            
            <button
              onClick={handleResummarize}
              disabled={!selectedTranscriptFile || isProcessing}
              className="mt-4 inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Generate New Summary
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
