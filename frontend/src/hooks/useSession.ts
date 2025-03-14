import { useState, useEffect, useCallback } from 'react';
import { Session } from '../types';

export interface UseSessionProps {
  setActiveSessionId: React.Dispatch<React.SetStateAction<string | null>>;
}

export const useSession = ({ setActiveSessionId }: UseSessionProps) => {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const fetchSessions = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await fetch('/sessions');
      if (!response.ok) {
        throw new Error(`Failed to fetch sessions: ${response.statusText}`);
      }
      const data = await response.json();
      setSessions(data.sessions);
      
      // If there's at least one session and no active session is set, set the first one as active
      if (data.sessions.length > 0) {
        setActiveSessionId(prevId => prevId || data.sessions[0].session_id);
      }
    } catch (error) {
      console.error('Error fetching sessions:', error);
      setError(error instanceof Error ? error.message : 'An unknown error occurred');
    } finally {
      setIsLoading(false);
    }
  }, [setActiveSessionId]);

  const createSession = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await fetch('/sessions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
      });
      if (!response.ok) {
        throw new Error(`Failed to create session: ${response.statusText}`);
      }
      const session = await response.json();
      setSessions(prev => [...prev, session]);
      setActiveSessionId(session.session_id);
      return session;
    } catch (error) {
      console.error('Error creating session:', error);
      setError(error instanceof Error ? error.message : 'An unknown error occurred');
      return null;
    } finally {
      setIsLoading(false);
    }
  }, [setActiveSessionId]);

  const deleteSession = useCallback(async (sessionId: string) => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await fetch(`/sessions/${sessionId}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error(`Failed to delete session: ${response.statusText}`);
      }
      setSessions(prev => prev.filter(s => s.session_id !== sessionId));
      
      // If the active session was deleted, set the first remaining session as active (if any)
      setActiveSessionId(prevId => {
        if (prevId === sessionId) {
          const remainingSessions = sessions.filter(s => s.session_id !== sessionId);
          return remainingSessions.length > 0 ? remainingSessions[0].session_id : null;
        }
        return prevId;
      });
      return true;
    } catch (error) {
      console.error('Error deleting session:', error);
      setError(error instanceof Error ? error.message : 'An unknown error occurred');
      return false;
    } finally {
      setIsLoading(false);
    }
  }, [sessions, setActiveSessionId]);

  const getSession = useCallback((sessionId: string | null) => {
    if (!sessionId) return null;
    return sessions.find(s => s.session_id === sessionId) || null;
  }, [sessions]);

  const updateSession = useCallback((session: Session) => {
    setSessions(prev => 
      prev.map(s => s.session_id === session.session_id ? session : s)
    );
  }, []);

  // Initial fetch of sessions
  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  return {
    sessions,
    isLoading,
    error,
    fetchSessions,
    createSession,
    deleteSession,
    getSession,
    updateSession
  };
};
