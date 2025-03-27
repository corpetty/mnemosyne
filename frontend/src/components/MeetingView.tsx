import React, { useState } from 'react';
import EnhancedNotes from './EnhancedNotes';
import { AudioDevice, Model, Session, TranscriptSegment } from '../types';

interface MeetingViewProps {
  sessionId: string | null;
  notes: string;
  transcript: TranscriptSegment[];
  devices: AudioDevice[];
  selectedDevices: string[];
  isRecording: boolean;
  isProcessing: boolean;
  models: Model[];
  selectedModel: string;
  processingStatus: string;
  sessions: Session[];
  onSaveNotes: (sessionId: string, notes: string) => Promise<boolean>;
  onDeviceToggle: (deviceId: string) => void;
  onStartRecording: () => Promise<void>;
  onStopRecording: () => Promise<void>;
  onModelChange: (modelId: string) => void;
  onSessionSelect: (sessionId: string) => void;
  onDeleteSession: (sessionId: string) => void;
  isSavingNotes: boolean;
  onCreateSession: () => Promise<void>;
  updateSession: (session: Session) => void;
}

const MeetingView: React.FC<MeetingViewProps> = ({
  sessionId,
  notes,
  transcript,
  devices,
  selectedDevices,
  isRecording,
  isProcessing,
  models,
  selectedModel,
  processingStatus,
  sessions,
  onSaveNotes,
  onDeviceToggle,
  onStartRecording,
  onStopRecording,
  onModelChange,
  onSessionSelect,
  onDeleteSession,
  isSavingNotes,
  onCreateSession,
  updateSession,
}) => {
  // All useState hooks at the top level
  const [showTranscript, setShowTranscript] = useState(false);
  const [isRenaming, setIsRenaming] = useState(false);
  
  // Find current session
  const currentSession = sessions.find(s => s.session_id === sessionId);
  const [sessionName, setSessionName] = useState('');
  
  // Update sessionName when currentSession changes
  React.useEffect(() => {
    if (currentSession?.name) {
      setSessionName(currentSession.name);
    }
  }, [currentSession]);

  // If no active session, show a prompt to create one
  if (!sessionId) {
    return (
      <div className="flex flex-col items-center justify-center py-16 space-y-4">
        <svg className="w-16 h-16 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
        <h3 className="text-lg font-medium text-gray-900">No Active Session</h3>
        <p className="text-gray-500 text-center max-w-md">
          You need to create or select a session to start taking notes and recording your meeting.
        </p>
        <button
          onClick={onCreateSession}
          className="mt-2 inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
        >
          <svg className="-ml-1 mr-2 h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
          </svg>
          Create New Session
        </button>
      </div>
    );
  }

  // Handle session rename
  const handleRenameSession = () => {
    if (!sessionId || !currentSession) return;
    
    const updatedSession = {
      ...currentSession,
      name: sessionName.trim() || `Session ${new Date(currentSession.created_at).toLocaleString()}`
    };
    updateSession(updatedSession);
    setIsRenaming(false);
  };

  // Handle session deletion with confirmation
  const handleDeleteConfirm = () => {
    if (!sessionId) return;
    
    if (window.confirm('Are you sure you want to delete this session? This action cannot be undone.')) {
      onDeleteSession(sessionId);
    }
  };

  return (
    <div className="space-y-6">
      {/* Session management header */}
      <div className="bg-white border border-gray-200 rounded-lg shadow-sm p-4">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
          <div className="flex-1">
            {isRenaming ? (
              <div className="flex items-center">
                <input
                  type="text"
                  value={sessionName}
                  onChange={e => setSessionName(e.target.value)}
                  className="px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm mr-2"
                  placeholder="Session name"
                  autoFocus
                />
                <button
                  onClick={handleRenameSession}
                  className="px-3 py-1 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700"
                >
                  Save
                </button>
                <button
                  onClick={() => setIsRenaming(false)}
                  className="ml-2 px-3 py-1 border border-gray-300 text-gray-700 text-sm rounded hover:bg-gray-50"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <div className="flex items-center">
                <span className="text-lg font-medium text-gray-900">
                  {currentSession?.name || `Session ${new Date(currentSession?.created_at || Date.now()).toLocaleString()}`}
                </span>
                <button
                  onClick={() => setIsRenaming(true)}
                  className="ml-2 text-gray-500 hover:text-gray-700"
                >
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                  </svg>
                </button>
              </div>
            )}
          </div>
          
          <div className="flex items-center space-x-3">
            <div>
              <select
                value={sessionId}
                onChange={(e) => onSessionSelect(e.target.value)}
                className="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm"
              >
                <option value="" disabled>Select Session</option>
                {sessions.map(session => (
                  <option key={session.session_id} value={session.session_id}>
                    {session.name || `Session ${new Date(session.created_at).toLocaleString()}`}
                  </option>
                ))}
              </select>
            </div>
            
            <button 
              onClick={onCreateSession}
              className="inline-flex items-center px-3 py-2 border border-transparent text-sm leading-4 font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
            >
              <svg className="-ml-0.5 mr-2 h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              New
            </button>
            
            <button
              onClick={handleDeleteConfirm}
              className="text-red-600 hover:text-red-800"
              title="Delete Session"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </div>
        </div>
        
        {isRecording && (
          <div className="mt-2 flex items-center">
            <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800">
              <span className="mr-1 h-2 w-2 rounded-full bg-red-500 animate-pulse"></span>
              Recording in progress
            </span>
          </div>
        )}
        
        {isProcessing && (
          <div className="mt-2 flex items-center">
            <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800">
              <svg className="animate-spin -ml-0.5 mr-1.5 h-2 w-2 text-yellow-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              Processing
            </span>
            {processingStatus && <span className="ml-2 text-xs text-gray-500">{processingStatus}</span>}
          </div>
        )}
      </div>
      
      <div className="flex justify-between items-center">
        <h1 className="text-xl font-semibold text-gray-800">Meeting Notes</h1>
        <button
          onClick={() => setShowTranscript(!showTranscript)}
          className="px-3 py-1 text-sm text-indigo-600 hover:text-indigo-800 
                   border border-indigo-100 hover:border-indigo-300 rounded-md 
                   transition-colors duration-150 focus:outline-none 
                   focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2"
        >
          {showTranscript ? 'Hide Transcript' : 'Show Transcript'}
        </button>
      </div>

      <div className="grid grid-cols-1 gap-6">
        {/* Main content area with notes editor and recording controls */}
        <div className="col-span-1">
          <EnhancedNotes 
            notes={notes}
            sessionId={sessionId}
            onSave={onSaveNotes}
            isSaving={isSavingNotes}
            isRecording={isRecording}
            isProcessing={isProcessing}
            devices={devices}
            selectedDevices={selectedDevices}
            models={models}
            selectedModel={selectedModel}
            processingStatus={processingStatus}
            onDeviceToggle={onDeviceToggle}
            onStartRecording={onStartRecording}
            onStopRecording={onStopRecording}
            onModelChange={onModelChange}
          />
        </div>

        {/* Collapsible transcript preview */}
        {showTranscript && (
          <div className="col-span-1 bg-white border border-gray-200 rounded-lg shadow-sm">
            <div className="flex justify-between items-center p-4 border-b border-gray-100">
              <h2 className="text-lg font-semibold text-gray-800 flex items-center">
                <svg className="h-5 w-5 mr-2 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                </svg>
                Live Transcript
              </h2>
            </div>
            <div className="p-4 max-h-60 overflow-y-auto">
              {transcript.length > 0 ? (
                <div className="space-y-3">
                  {transcript.map((segment, index) => (
                    <div key={index} className="pb-2 border-b border-gray-100 last:border-0">
                      <div className="flex items-start">
                        <div className="flex-shrink-0 mt-0.5">
                          <span className="inline-flex items-center justify-center h-6 w-6 rounded-full bg-indigo-100 text-indigo-800 text-xs font-medium">
                            {segment.speaker?.substring(0, 1) || '?'}
                          </span>
                        </div>
                        <div className="ml-3">
                          <p className="text-xs font-medium text-gray-900">{segment.speaker || 'Unknown'}</p>
                          <p className="mt-1 text-sm text-gray-600">{segment.text}</p>
                          <p className="mt-1 text-xs text-gray-400">
                            {segment.start && new Date(segment.start * 1000).toLocaleTimeString()} - 
                            {segment.end && new Date(segment.end * 1000).toLocaleTimeString()}
                          </p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-center py-4 text-gray-500">
                  {isRecording ? (
                    <p>Recording in progress. Transcript will appear here...</p>
                  ) : (
                    <p>No transcript available. Start recording to see the transcript.</p>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default MeetingView;
