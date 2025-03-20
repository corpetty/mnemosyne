import React, { useState } from 'react';
import { Session, SessionStatus } from '../types';

interface SessionListProps {
  sessions: Session[];
  activeSessionId: string | null;
  onSessionSelect: (sessionId: string) => void;
  onCreateSession: () => void;
  onDeleteSession: (sessionId: string) => void;
  isLoading: boolean;
  updateSession?: (session: Session) => void;
}

interface RenameFormProps {
  sessionId: string;
  currentName: string | undefined;
  onRename: (sessionId: string, newName: string) => void;
  onCancel: () => void;
}

const RenameForm: React.FC<RenameFormProps> = ({ sessionId, currentName, onRename, onCancel }) => {
  const [newName, setNewName] = useState(currentName || '');
  
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (newName.trim()) {
      onRename(sessionId, newName.trim());
    }
  };
  
  return (
    <form onSubmit={handleSubmit} className="flex items-center mt-2 mb-2">
      <input
        type="text"
        value={newName}
        onChange={(e) => setNewName(e.target.value)}
        className="flex-1 px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm"
        placeholder="Enter session name"
        autoFocus
      />
      <button 
        type="submit" 
        className="ml-3 inline-flex items-center px-3 py-2 border border-transparent text-sm leading-4 font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
      >
        Save
      </button>
      <button 
        type="button" 
        onClick={onCancel}
        className="ml-2 inline-flex items-center px-3 py-2 border border-gray-300 text-sm leading-4 font-medium rounded-md shadow-sm text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
      >
        Cancel
      </button>
    </form>
  );
};

const SessionList: React.FC<SessionListProps> = ({
  sessions,
  activeSessionId,
  onSessionSelect,
  onCreateSession,
  onDeleteSession,
  isLoading,
  updateSession
}) => {
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  
  // Function to handle rename
  const handleRename = async (sessionId: string, newName: string) => {
    try {
      const response = await fetch(`/sessions/${sessionId}/rename`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ name: newName }),
      });
      
      if (!response.ok) {
        throw new Error('Failed to rename session');
      }
      
      // Get the updated session data
      const updatedData = await response.json();
      
      // Update the local state using updateSession
      if (updateSession) {
        const session = sessions.find(s => s.session_id === sessionId);
        if (session) {
          // Create an updated session object with the new name
          const updatedSession = {
            ...session,
            name: newName
          };
          updateSession(updatedSession);
        }
      }
      
      // Reset renaming state
      setRenamingSessionId(null);
    } catch (error) {
      console.error('Error renaming session:', error);
    }
  };
  
  // Get a human-readable status with icon
  const getStatusDisplay = (status: string) => {
    switch(status) {
      case SessionStatus.Created:
        return <span className="inline-flex h-3 w-3 rounded-full bg-gray-300"></span>;
      case SessionStatus.Recording:
        return <span className="inline-flex h-3 w-3 rounded-full bg-red-500 animate-pulse"></span>;
      case SessionStatus.Processing:
        return <span className="inline-flex h-3 w-3 rounded-full bg-yellow-400"></span>;
      case SessionStatus.Completed:
        return <span className="inline-flex items-center justify-center h-5 w-5 rounded-full bg-green-100 text-green-600">
          <svg className="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
          </svg>
        </span>;
      case SessionStatus.Error:
        return <span className="inline-flex items-center justify-center h-5 w-5 rounded-full bg-red-100 text-red-600">
          <svg className="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
          </svg>
        </span>;
      default:
        return <span className="inline-flex h-3 w-3 rounded-full bg-gray-300"></span>;
    }
  };
  
  // Format created_at date
  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };
  
  // Get session name/label
  const getSessionName = (session: Session, index: number) => {
    const shortId = session.session_id.substring(0, 6);
    
    // Show status-specific messages for active sessions
    if (session.status === SessionStatus.Recording) {
      return `Recording... (${shortId})`;
    }
    
    if (session.status === SessionStatus.Processing) {
      return `Processing... (${shortId})`;
    }
    
    // If session has a custom name, use it
    if (session.name) {
      return `${session.name} (${shortId})`;
    }
    
    // Default fallback
    return `Session ${index + 1} (${shortId})`;
  };

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden">
      {sessions.length === 0 ? (
        <div className="text-gray-500 text-center py-8 px-4">
          {isLoading ? (
            <div className="animate-pulse flex flex-col items-center">
              <div className="h-4 bg-gray-200 rounded w-1/4 mb-4"></div>
              <div className="h-4 bg-gray-200 rounded w-1/2"></div>
            </div>
          ) : (
            <div className="flex flex-col items-center">
              <svg className="h-12 w-12 text-gray-400 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
              </svg>
              <p>No sessions available. Create one to start.</p>
              <button
                onClick={onCreateSession}
                className="mt-4 px-4 py-2 bg-indigo-600 text-white text-sm rounded-md hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
              >
                Create Session
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className="overflow-auto max-h-[600px]">
          <ul className="divide-y divide-gray-200">
            {sessions.map((session, index) => (
              <li key={session.session_id}>
                {renamingSessionId === session.session_id ? (
                  <div className="p-4 bg-gray-50">
                    <RenameForm 
                      sessionId={session.session_id}
                      currentName={session.name}
                      onRename={handleRename}
                      onCancel={() => setRenamingSessionId(null)}
                    />
                  </div>
                ) : (
                  <div 
                    className={`p-4 hover:bg-gray-50 transition-colors duration-150 ${
                      session.session_id === activeSessionId ? 'bg-indigo-50 border-l-4 border-indigo-500' : ''
                    }`}
                    onClick={() => onSessionSelect(session.session_id)}
                  >
                    <div className="sm:flex sm:items-center sm:justify-between">
                      <div className="flex items-center space-x-3">
                        <div className="flex-shrink-0">
                          {getStatusDisplay(session.status)}
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="text-sm font-medium text-gray-900 truncate">
                            {getSessionName(session, index)}
                          </p>
                          <p className="text-xs text-gray-500">
                            Created {formatDate(session.created_at)}
                          </p>
                        </div>
                      </div>
                      <div className="mt-2 sm:mt-0 flex items-center space-x-2">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setRenamingSessionId(session.session_id);
                          }}
                          className="inline-flex items-center p-1 border border-transparent rounded-full text-indigo-600 hover:bg-indigo-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
                          title="Rename session"
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                          </svg>
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            onDeleteSession(session.session_id);
                          }}
                          className="inline-flex items-center p-1 border border-transparent rounded-full text-red-600 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500"
                          title="Delete session"
                        >
                          <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                          </svg>
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

export default SessionList;
