import React, { useState, useEffect, useCallback } from 'react';
import DeviceSelection from './components/DeviceSelection';
import Transcript from './components/Transcript';
import Summary from './components/Summary';
import SessionList from './components/SessionList';
import { useDevices, useWebSocket, useSession } from './hooks';
import { TranscriptSegment, UploadMode, Model, TranscriptFile, Session } from './types';

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
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);

  // Session management
  const { 
    sessions, 
    isLoading: isSessionsLoading, 
    error: sessionsError,
    fetchSessions,
    createSession,
    deleteSession,
    getSession,
    updateSession
  } = useSession({ setActiveSessionId });
  
  const { devices, fetchDevices, isLoading: isDevicesLoading } = useDevices(setSelectedDevices);
  
  const webSocketProps = {
    setTranscript,
    setSummary,
    setIsProcessing,
    setProcessingStatus,
    activeSessionId,
    updateSession
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

  // Create a new session
  const handleCreateSession = async () => {
    const session = await createSession();
    if (session) {
      setActiveSessionId(session.session_id);
      // Reset UI for new session
      setTranscript([]);
      setSummary('');
      setProcessingStatus('');
      setIsRecording(false);
      setIsProcessing(false);
    }
  };
  
  // Handle session selection
  const handleSessionSelect = async (sessionId: string) => {
    setActiveSessionId(sessionId);
    // Reset UI for new active session
    setTranscript([]);
    setSummary('');
    
    // Update UI state based on session status
    const session = getSession(sessionId);
    if (session) {
      setIsRecording(session.is_recording);
      setIsProcessing(session.status === 'processing');
      setProcessingStatus(session.status === 'processing' ? 'Processing...' : '');
      
      // If the session has a transcript file, load the data
      if (session.transcript_file) {
        try {
          // Manually load the transcript file content with include_data=true
          setProcessingStatus('Loading transcript and summary...');
          const response = await fetch(`/sessions/${sessionId}?include_data=true`);
          if (response.ok) {
            const sessionData = await response.json();
            
            // Update UI with transcript and summary
            if (sessionData.transcript && sessionData.transcript.length > 0) {
              setTranscript(sessionData.transcript);
            }
            
            if (sessionData.summary) {
              setSummary(sessionData.summary);
            }
            
            setProcessingStatus('');
          }
        } catch (error) {
          console.error('Error loading session data:', error);
          setProcessingStatus('Error loading session data');
        }
      }
    }
  };
  
  // Handle session deletion
  const handleDeleteSession = async (sessionId: string) => {
    if (window.confirm('Are you sure you want to delete this session?')) {
      await deleteSession(sessionId);
    }
  };

  const startRecording = async () => {
    try {
      console.log('Starting recording with devices:', selectedDevices);
      
      // Create a new session if none is active
      let sessionId = activeSessionId;
      if (!sessionId) {
        const session = await createSession();
        if (session) {
          sessionId = session.session_id;
          setActiveSessionId(sessionId);
        } else {
          throw new Error('Failed to create a new session');
        }
      }
      
      const result = await sendStartRecording(selectedDevices, selectedModel || undefined);
      setIsRecording(true);
      setTranscript([]);
      setSummary('');
      setProcessingStatus('');
      
      // Refresh sessions to get updated status
      fetchSessions();
    } catch (error) {
      console.error('Error starting recording:', error);
      setProcessingStatus('Error starting recording');
    }
  };

  const stopRecording = async () => {
    try {
      setIsProcessing(true);
      setProcessingStatus('Stopping recording...');
      const result = await sendStopRecording();
      setIsRecording(false);
      setProcessingStatus('Processing audio...');
      
      // Refresh sessions to get updated status
      fetchSessions();
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
      // Create a new session if none is active
      if (!activeSessionId) {
        const session = await createSession();
        if (session) {
          setActiveSessionId(session.session_id);
        }
      }
      
      setIsProcessing(true);
      setProcessingStatus('Uploading file...');
      const result = await sendUploadFile(file);
      setProcessingStatus('Processing uploaded file...');
      
      // Refresh the transcript files list after upload
      setTimeout(() => {
        fetchTranscriptFiles();
        fetchSessions();
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
      // Create a new session if none is active
      if (!activeSessionId) {
        const session = await createSession();
        if (session) {
          setActiveSessionId(session.session_id);
        }
      }
      
      setProcessingStatus('Generating new summary...');
      setTranscript([]);
      
      const result = await resummarizeTranscript(selectedTranscriptFile, selectedModel || undefined);
      
      // The summary will be updated through the WebSocket connection
      console.log('Resummarization completed:', result);
      
      // Refresh sessions to get updated status
      fetchSessions();
    } catch (error) {
      console.error('Error resummarizing:', error);
      setProcessingStatus('Error generating new summary');
    }
  };

  const handleRefreshDevices = useCallback(() => {
    fetchDevices(true);
  }, [fetchDevices]);

  const isLoading = isDevicesLoading || isSessionsLoading;

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
                {isDevicesLoading ? 'Refreshing...' : 'Refresh Devices'}
              </button>
            </div>
            
            {/* Sessions List */}
            <SessionList 
              sessions={sessions}
              activeSessionId={activeSessionId}
              onSessionSelect={handleSessionSelect}
              onCreateSession={handleCreateSession}
              onDeleteSession={handleDeleteSession}
              isLoading={isSessionsLoading}
            />
            
            {isDevicesLoading ? (
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
