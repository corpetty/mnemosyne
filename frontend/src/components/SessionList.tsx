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
    <form onSubmit={handleSubmit} className="flex items-center mt-1 mb-2">
      <input
        type="text"
        value={newName}
        onChange={(e) => setNewName(e.target.value)}
        className="flex-1 px-2 py-1 border border-gray-300 rounded mr-2"
        placeholder="Enter session name"
        autoFocus
      />
      <button 
        type="submit" 
        className="px-2 py-1 bg-blue-500 text-white text-xs rounded hover:bg-blue-600"
      >
        Save
      </button>
      <button 
        type="button" 
        onClick={onCancel}
        className="px-2 py-1 bg-gray-300 text-gray-700 text-xs rounded hover:bg-gray-400 ml-1"
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
        return <span className="text-gray-500">●</span>;
      case SessionStatus.Recording:
        return <span className="text-red-500 animate-pulse">⬤</span>;
      case SessionStatus.Processing:
        return <span className="text-yellow-500">⬤</span>;
      case SessionStatus.Completed:
        return <span className="text-green-500">✓</span>;
      case SessionStatus.Error:
        return <span className="text-red-500">✕</span>;
      default:
        return <span className="text-gray-500">●</span>;
    }
  };
  
  // Format created_at date
  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleTimeString();
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
    <div className="bg-white shadow rounded-lg p-4 mb-6">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-lg font-medium text-gray-900">Sessions</h2>
        <button
          onClick={onCreateSession}
          disabled={isLoading}
          className="px-3 py-1 bg-blue-500 text-white text-sm rounded hover:bg-blue-600 disabled:bg-blue-300"
        >
          New Session
        </button>
      </div>

      {sessions.length === 0 ? (
        <div className="text-gray-500 text-center py-4">
          {isLoading ? 'Loading sessions...' : 'No sessions available. Create one to start.'}
        </div>
      ) : (
        <div className="overflow-auto max-h-60">
          <ul className="divide-y divide-gray-200">
            {sessions.map((session, index) => (
              <li key={session.session_id} className={`py-2 px-1 hover:bg-gray-50 ${session.session_id === activeSessionId ? 'bg-blue-50' : ''}`}>
                {renamingSessionId === session.session_id ? (
                  <RenameForm 
                    sessionId={session.session_id}
                    currentName={session.name}
                    onRename={handleRename}
                    onCancel={() => setRenamingSessionId(null)}
                  />
                ) : (
                  <div className="flex items-center justify-between cursor-pointer" onClick={() => onSessionSelect(session.session_id)}>
                    <div className="flex items-center space-x-2">
                      <div className="w-4">{getStatusDisplay(session.status)}</div>
                      <div className="flex-1 truncate">{getSessionName(session, index)}</div>
                      <div className="text-xs text-gray-500">{formatDate(session.created_at)}</div>
                    </div>
                    <div className="flex items-center">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setRenamingSessionId(session.session_id);
                        }}
                        className="text-blue-500 hover:text-blue-700 text-sm mr-2"
                      >
                        Rename
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeleteSession(session.session_id);
                        }}
                        className="text-red-500 hover:text-red-700 text-sm"
                      >
                        Delete
                      </button>
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
