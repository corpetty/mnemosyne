import React, { useState, useCallback } from 'react';
import DeviceSelection from './components/DeviceSelection';
import Transcript from './components/Transcript';
import Summary from './components/Summary';
import SessionList from './components/SessionList';
import ParticipantList from './components/ParticipantList';
import Header from './components/Header';
import Layout from './components/Layout';
import { useDevices, useWebSocket, useSession } from './hooks';
import { TranscriptSegment, UploadMode } from './types';

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
  const [activeTab, setActiveTab] = useState<string>('sessions');

  // Session management
  const { 
    sessions, 
    isLoading: isSessionsLoading,
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
      
      await sendStartRecording(selectedDevices, selectedModel || undefined);
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
      await sendStopRecording();
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
      await sendUploadFile(file);
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

  // Handle tab change
  const handleTabChange = (tab: string) => {
    setActiveTab(tab);
  };

  const renderTabContent = () => {
    switch (activeTab) {
      case 'recordings':
        return (
          <div className="space-y-6">
            <h2 className="text-xl font-semibold text-gray-800 border-b pb-2">Recording Studio</h2>
            {isDevicesLoading ? (
              <div className="p-4 bg-gray-50 rounded animate-pulse">
                <p className="text-gray-500">Loading audio devices...</p>
              </div>
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
          </div>
        );
      case 'settings':
        return (
          <div className="space-y-6">
            <h2 className="text-xl font-semibold text-gray-800 border-b pb-2">Settings</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="bg-white p-4 rounded-lg shadow-sm border border-gray-200">
                <h3 className="font-medium text-gray-700 mb-2">Device Management</h3>
                <button
                  onClick={handleRefreshDevices}
                  disabled={isLoading}
                  className="px-4 py-2 bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:bg-indigo-300 text-sm"
                >
                  {isDevicesLoading ? 'Refreshing...' : 'Refresh Audio Devices'}
                </button>
              </div>
              
              <div className="bg-white p-4 rounded-lg shadow-sm border border-gray-200">
                <h3 className="font-medium text-gray-700 mb-2">Model Selection</h3>
                <div className="text-sm text-gray-600">
                  {models.length > 0 ? (
                    <div>
                      <p className="mb-2">Available models:</p>
                      <ul className="list-disc pl-5">
                        {models.map(model => (
                          <li key={model.id}>{model.name}</li>
                        ))}
                      </ul>
                    </div>
                  ) : (
                    <p>No models available</p>
                  )}
                </div>
              </div>
            </div>
          </div>
        );
      default: // 'sessions' tab
        return (
          <div className="space-y-6">
            <div className="flex justify-between items-center">
              <h2 className="text-xl font-semibold text-gray-800">All Sessions</h2>
              <button
                onClick={handleCreateSession}
                disabled={isSessionsLoading}
                className="px-3 py-1 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700 disabled:bg-indigo-300"
              >
                New Session
              </button>
            </div>
            
            <SessionList 
              sessions={sessions}
              activeSessionId={activeSessionId}
              onSessionSelect={handleSessionSelect}
              onCreateSession={handleCreateSession}
              onDeleteSession={handleDeleteSession}
              isLoading={isSessionsLoading}
              updateSession={updateSession}
            />
            
            {activeSessionId && getSession(activeSessionId) && (
              <div className="mt-8 space-y-6">
                <div className="border-t border-gray-200 pt-4">
                  <h3 className="text-lg font-medium text-gray-800">Session Details</h3>
                </div>
                <ParticipantList 
                  participants={getSession(activeSessionId)?.participants || []} 
                  sessionId={activeSessionId}
                  onParticipantsUpdated={() => {
                    // Refresh sessions to get updated participant data
                    fetchSessions();
                  }}
                />
                
                <div className="bg-gray-50 p-4 rounded-lg">
                  <h3 className="text-lg font-medium text-gray-800 mb-4">Transcript</h3>
                  <Transcript transcript={transcript} />
                </div>
                
                {summary && (
                  <div className="bg-gray-50 p-4 rounded-lg">
                    <h3 className="text-lg font-medium text-gray-800 mb-4">Summary</h3>
                    <Summary summary={summary} processingStatus={processingStatus} />
                  </div>
                )}
              </div>
            )}
          </div>
        );
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <Header 
        activeTab={activeTab} 
        onTabChange={handleTabChange} 
      />
      
      <Layout>
        {renderTabContent()}
      </Layout>
    </div>
  );
}

export default App;
