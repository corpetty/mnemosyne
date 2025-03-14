import React from 'react';
import { Session, SessionStatus } from '../types';

interface SessionListProps {
  sessions: Session[];
  activeSessionId: string | null;
  onSessionSelect: (sessionId: string) => void;
  onCreateSession: () => void;
  onDeleteSession: (sessionId: string) => void;
  isLoading: boolean;
}

const SessionList: React.FC<SessionListProps> = ({
  sessions,
  activeSessionId,
  onSessionSelect,
  onCreateSession,
  onDeleteSession,
  isLoading
}) => {
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
    
    if (session.status === SessionStatus.Recording) {
      return `Recording... (${shortId})`;
    }
    
    if (session.status === SessionStatus.Processing) {
      return `Processing... (${shortId})`;
    }
    
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
              <li key={session.session_id} className={`py-2 px-1 cursor-pointer hover:bg-gray-50 ${session.session_id === activeSessionId ? 'bg-blue-50' : ''}`}>
                <div className="flex items-center justify-between" onClick={() => onSessionSelect(session.session_id)}>
                  <div className="flex items-center space-x-2">
                    <div className="w-4">{getStatusDisplay(session.status)}</div>
                    <div className="flex-1 truncate">{getSessionName(session, index)}</div>
                    <div className="text-xs text-gray-500">{formatDate(session.created_at)}</div>
                  </div>
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
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

export default SessionList;
