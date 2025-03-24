import { useEffect, useCallback, useState, useRef } from 'react';
import { TranscriptSegment, Model, TranscriptFile, Session } from '../types';

interface UseWebSocketProps {
  setTranscript: React.Dispatch<React.SetStateAction<TranscriptSegment[]>>;
  setSummary: React.Dispatch<React.SetStateAction<string>>;
  setNotes: React.Dispatch<React.SetStateAction<string>>;
  setIsProcessing: React.Dispatch<React.SetStateAction<boolean>>;
  setProcessingStatus: React.Dispatch<React.SetStateAction<string>>;
  activeSessionId: string | null;
  updateSession?: (session: Session) => void;
}

export const useWebSocket = ({
  setTranscript,
  setSummary,
  setNotes,
  setIsProcessing,
  setProcessingStatus,
  activeSessionId,
  updateSession,
}: UseWebSocketProps) => {
  const [models, setModels] = useState<Model[]>([]);
  const [transcriptFiles, setTranscriptFiles] = useState<TranscriptFile[]>([]);
  const websocketRef = useRef<WebSocket | null>(null);
  const fetchModels = useCallback(async () => {
    try {
      const response = await fetch('/models');
      if (response.ok) {
        const data = await response.json();
        setModels(data.models);
      }
    } catch (error) {
      console.error('Error fetching models:', error);
    }
  }, []);

  const fetchTranscriptFiles = useCallback(async () => {
    try {
      const response = await fetch('/transcripts');
      if (response.ok) {
        const data = await response.json();
        setTranscriptFiles(data.transcripts);
      }
    } catch (error) {
      console.error('Error fetching transcript files:', error);
    }
  }, []);

  const sendWebSocketMessage = useCallback((message: any) => {
    if (websocketRef.current && websocketRef.current.readyState === WebSocket.OPEN) {
      websocketRef.current.send(JSON.stringify(message));
      return true;
    }
    return false;
  }, []);

  const subscribeToSession = useCallback((sessionId: string) => {
    console.log(`Subscribing to session: ${sessionId}`);
    // Clear current state when switching sessions
    setTranscript([]);
    setSummary('');
    setNotes('');
    
    sendWebSocketMessage({
      type: 'subscribe',
      session_id: sessionId
    });
  }, [sendWebSocketMessage, setTranscript, setSummary, setNotes]);

  const unsubscribeFromSession = useCallback(() => {
    sendWebSocketMessage({
      type: 'unsubscribe'
    });
  }, [sendWebSocketMessage]);

  const initWebSocket = useCallback(() => {
    // Close existing connection if it exists
    if (websocketRef.current) {
      websocketRef.current.close();
    }

    const websocket = new WebSocket('ws://localhost:8000/ws');
    websocketRef.current = websocket;

    websocket.onopen = () => {
      console.log('WebSocket Connected');
      
      // Subscribe to active session if available
      if (activeSessionId) {
        subscribeToSession(activeSessionId);
      }
    };

    websocket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      
      // Skip ping messages
      if (data.type === 'ping') {
        return;
      }
      
      // Only process messages for the active session or messages without a session_id
      const isActiveSessionMessage = !data.session_id || data.session_id === activeSessionId;
      
      if (data.type === 'transcription' && isActiveSessionMessage) {
        setTranscript(prev => [...prev, data.data]);
      } else if (data.type === 'summary' && isActiveSessionMessage) {
        setSummary(data.data);
        setIsProcessing(false);
        setProcessingStatus('');
      } else if (data.type === 'notes_updated' && isActiveSessionMessage) {
        setNotes(data.notes);
      } else if (data.type === 'status') {
        if (isActiveSessionMessage) {
          setProcessingStatus(data.message);
        }
        
        // Update session status if we have a session ID and an update function
        if (data.session_id && updateSession) {
          // Fetch the updated session details
          fetch(`/sessions/${data.session_id}`)
            .then(response => response.json())
            .then(session => {
              updateSession(session);
            })
            .catch(error => {
              console.error(`Error fetching session ${data.session_id}:`, error);
            });
        }
      } else if (data.type === 'subscribed') {
        console.log(`Successfully subscribed to session: ${data.session_id}`);
      } else if (data.type === 'error') {
        console.error(`WebSocket error: ${data.message}`);
      }
    };

    websocket.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    websocket.onclose = () => {
      console.log('WebSocket disconnected');
      // Try to reconnect after a delay
      setTimeout(() => {
        if (websocketRef.current === websocket) { // Only reconnect if this is still the current websocket
          initWebSocket();
        }
      }, 5000);
    };

    return () => {
      websocket.close();
    };
  }, [activeSessionId, setTranscript, setSummary, setNotes, setIsProcessing, setProcessingStatus, subscribeToSession, updateSession]);

  // Initialize WebSocket connection
  useEffect(() => {
    const cleanup = initWebSocket();
    return cleanup;
  }, [initWebSocket]);
  
  // Subscribe to active session whenever it changes
  useEffect(() => {
    if (activeSessionId && websocketRef.current?.readyState === WebSocket.OPEN) {
      // Unsubscribe from current session first
      unsubscribeFromSession();
      // Then subscribe to the new session
      subscribeToSession(activeSessionId);
    }
  }, [activeSessionId, subscribeToSession, unsubscribeFromSession]);

  const sendStartRecording = async (selectedDevices: string[], model?: string) => {
    const body: any = {
      device_ids: selectedDevices,
      model: model
    };
    
    // Include session ID if available
    if (activeSessionId) {
      body.session_id = activeSessionId;
    }
    
    const response = await fetch('/start', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    
    if (!response.ok) {
      throw new Error('Failed to start recording');
    }
    
    const result = await response.json();
    return result;
  };

  const sendStopRecording = async () => {
    let url = '/stop';
    
    // Include session ID if available
    if (activeSessionId) {
      url += `?session_id=${encodeURIComponent(activeSessionId)}`;
    }
    
    const response = await fetch(url, {
      method: 'POST',
    });
    
    if (!response.ok) {
      throw new Error('Failed to stop recording');
    }
    
    const result = await response.json();
    return result;
  };

  const sendUploadFile = async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    
    // Include session ID if available
    if (activeSessionId) {
      formData.append('session_id', activeSessionId);
    }

    const response = await fetch('/upload', {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      throw new Error('Failed to upload file');
    }
    
    const result = await response.json();
    return result;
  };

  const resummarizeTranscript = async (transcriptFile: string, model?: string) => {
    try {
      setIsProcessing(true);
      setProcessingStatus('Generating new summary...');
      
      const body: any = {
        transcript_file: transcriptFile,
        model: model
      };
      
      // Include session ID if available
      if (activeSessionId) {
        body.session_id = activeSessionId;
      }
      
      const response = await fetch('/resummarize', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      });
      
      if (!response.ok) {
        throw new Error('Failed to resummarize transcript');
      }
      
      const result = await response.json();
      setProcessingStatus('');
      return result;
    } catch (error) {
      console.error('Error resummarizing transcript:', error);
      setProcessingStatus('Error generating new summary');
      throw error;
    } finally {
      setIsProcessing(false);
    }
  };

  // Fetch models and transcript files on initial load
  useEffect(() => {
    fetchModels();
    fetchTranscriptFiles();
  }, [fetchModels, fetchTranscriptFiles]);

  return { 
    initWebSocket, 
    sendStartRecording, 
    sendStopRecording, 
    sendUploadFile,
    resummarizeTranscript,
    fetchModels,
    fetchTranscriptFiles,
    subscribeToSession,
    unsubscribeFromSession,
    models,
    transcriptFiles
  };
};
