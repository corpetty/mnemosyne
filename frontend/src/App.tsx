import React, { useState, useEffect, useCallback } from 'react';
import DeviceSelection from './components/DeviceSelection';
import Transcript from './components/Transcript';
import Summary from './components/Summary';
import { useDevices, useWebSocket } from './hooks';
import { TranscriptSegment, UploadMode, Model, TranscriptFile } from './types';

function App() {
  const [isRecording, setIsRecording] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptSegment[]>([]);
  const [summary, setSummary] = useState<string>('');
  const [selectedDevices, setSelectedDevices] = useState<Array<string>>([]);
  const [processingStatus, setProcessingStatus] = useState<string>('');
  const [uploadMode, setUploadMode] = useState<UploadMode>(UploadMode.Recording);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [selectedTranscriptFile, setSelectedTranscriptFile] = useState<string>('');

  const { devices, fetchDevices, isLoading } = useDevices(setSelectedDevices);
  const webSocketProps = {
    setTranscript,
    setSummary,
    setIsProcessing,
    setProcessingStatus,
  };
  const { 
    initWebSocket, 
    sendStartRecording, 
    sendStopRecording, 
    sendUploadFile, 
    resummarizeTranscript,
    models,
    transcriptFiles,
    fetchTranscriptFiles
  } = useWebSocket(webSocketProps);

  useEffect(() => {
    initWebSocket();
  }, [initWebSocket]);

  const startRecording = async () => {
    try {
      console.log('Starting recording with devices:', selectedDevices);
      await sendStartRecording(selectedDevices, selectedModel || undefined);
      setIsRecording(true);
      setTranscript([]);
      setSummary('');
      setProcessingStatus('');
    } catch (error) {
      console.error('Error starting recording:', error);
      setProcessingStatus('Error starting recording');
    }
  };

  const stopRecording = async () => {
    try {
      setIsProcessing(true);
      setProcessingStatus('Stopping recording...');
      await sendStopRecording();
      setIsRecording(false);
      setProcessingStatus('Processing audio...');
    } catch (error) {
      console.error('Error stopping recording:', error);
      setIsProcessing(false);
      setProcessingStatus('Error processing recording');
    }
  };

  const handleDeviceToggle = (deviceId: string) => {
    setSelectedDevices(prev => {
      if (prev.includes(deviceId)) {
        return prev.filter(id => id !== deviceId);
      } else {
        return [...prev, deviceId];
      }
    });
  };

  const handleUploadFile = async (file: File) => {
    try {
      setIsProcessing(true);
      setProcessingStatus('Uploading file...');
      await sendUploadFile(file);
      setProcessingStatus('Processing uploaded file...');
      
      // Refresh the transcript files list after upload
      setTimeout(() => {
        fetchTranscriptFiles();
      }, 2000);
    } catch (error) {
      console.error('Error uploading file:', error);
      setIsProcessing(false);
      setProcessingStatus('Error uploading file');
    }
  };
  
  const handleResummarize = async () => {
    if (!selectedTranscriptFile) {
      setProcessingStatus('Please select a transcript file');
      return;
    }
    
    try {
      setProcessingStatus('Generating new summary...');
      setTranscript([]);
      
      const result = await resummarizeTranscript(selectedTranscriptFile, selectedModel || undefined);
      
      // The summary will be updated through the WebSocket connection
      console.log('Resummarization completed:', result);
    } catch (error) {
      console.error('Error resummarizing:', error);
      setProcessingStatus('Error generating new summary');
    }
  };

  const handleRefreshDevices = useCallback(() => {
    fetchDevices(true);
  }, [fetchDevices]);

  return (
    <div className="min-h-screen bg-gray-100">
      <div className="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <div className="px-4 py-6 sm:px-0">
          <div className="bg-white shadow rounded-lg p-6">
            <div className="flex justify-between items-center mb-6">
              <h1 className="text-2xl font-bold text-gray-900">
                Audio Transcriber
              </h1>
              <button
                onClick={handleRefreshDevices}
                disabled={isLoading}
                className="px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:bg-blue-300"
              >
                {isLoading ? 'Refreshing...' : 'Refresh Devices'}
              </button>
            </div>
            {isLoading ? (
              <p>Loading audio devices...</p>
            ) : (
              <DeviceSelection
                devices={devices}
                selectedDevices={selectedDevices}
                isRecording={isRecording}
                isProcessing={isProcessing}
                uploadMode={uploadMode}
                handleDeviceToggle={handleDeviceToggle}
                setUploadMode={setUploadMode}
                startRecording={startRecording}
                stopRecording={stopRecording}
                handleUploadFile={handleUploadFile}
                processingStatus={processingStatus}
                models={models}
                selectedModel={selectedModel}
                setSelectedModel={setSelectedModel}
                transcriptFiles={transcriptFiles}
                selectedTranscriptFile={selectedTranscriptFile}
                setSelectedTranscriptFile={setSelectedTranscriptFile}
                handleResummarize={handleResummarize}
              />
            )}
            <div className="space-y-6 mt-6">
              <Transcript transcript={transcript} />
              {summary && <Summary summary={summary} processingStatus={processingStatus} />}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
