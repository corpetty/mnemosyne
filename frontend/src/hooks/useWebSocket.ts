import { useEffect, useCallback } from 'react';
import { TranscriptSegment } from '../types';

interface UseWebSocketProps {
  setTranscript: React.Dispatch<React.SetStateAction<TranscriptSegment[]>>;
  setSummary: React.Dispatch<React.SetStateAction<string>>;
  setIsProcessing: React.Dispatch<React.SetStateAction<boolean>>;
  setProcessingStatus: React.Dispatch<React.SetStateAction<string>>;
}

export const useWebSocket = ({
  setTranscript,
  setSummary,
  setIsProcessing,
  setProcessingStatus,
}: UseWebSocketProps) => {
  const initWebSocket = useCallback(() => {
    const websocket = new WebSocket('ws://localhost:8000/ws');

    websocket.onopen = () => {
      console.log('WebSocket Connected');
    };

    websocket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'transcription') {
        setTranscript(prev => [...prev, data.data]);
      } else if (data.type === 'summary') {
        setSummary(data.data);
        setIsProcessing(false);
        setProcessingStatus('');
      } else if (data.type === 'status') {
        setProcessingStatus(data.message);
      }
    };

    websocket.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    return () => {
      websocket.close();
    };
  }, [setTranscript, setSummary, setIsProcessing, setProcessingStatus]);

  useEffect(() => {
    initWebSocket();
  }, [initWebSocket]);

  const sendStartRecording = async (selectedDevices: string[]) => {
    const response = await fetch('http://localhost:8000/start', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(selectedDevices),
    });
    if (!response.ok) {
      throw new Error('Failed to start recording');
    }
  };

  const sendStopRecording = async () => {
    const response = await fetch('http://localhost:8000/stop', {
      method: 'POST',
    });
    if (!response.ok) {
      throw new Error('Failed to stop recording');
    }
  };

  const sendUploadFile = async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);

    const response = await fetch('http://localhost:8000/upload', {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      throw new Error('Failed to upload file');
    }
  };

  return { initWebSocket, sendStartRecording, sendStopRecording, sendUploadFile };
};
