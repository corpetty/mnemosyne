import { useEffect, useCallback, useState } from 'react';
import { TranscriptSegment, Model, TranscriptFile } from '../types';

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
  const [models, setModels] = useState<Model[]>([]);
  const [transcriptFiles, setTranscriptFiles] = useState<TranscriptFile[]>([]);
  const fetchModels = useCallback(async () => {
    try {
      const response = await fetch('http://localhost:8000/models');
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
      const response = await fetch('http://localhost:8000/transcripts');
      if (response.ok) {
        const data = await response.json();
        setTranscriptFiles(data.transcripts);
      }
    } catch (error) {
      console.error('Error fetching transcript files:', error);
    }
  }, []);

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

  const sendStartRecording = async (selectedDevices: string[], model?: string) => {
    // Create request body with model parameter
    const response = await fetch('http://localhost:8000/start', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        device_ids: selectedDevices,
        model: model
      }),
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

  const resummarizeTranscript = async (transcriptFile: string, model?: string) => {
    try {
      setIsProcessing(true);
      setProcessingStatus('Generating new summary...');
      
      const response = await fetch('http://localhost:8000/resummarize', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          transcript_file: transcriptFile,
          model: model
        }),
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
    models,
    transcriptFiles
  };
};
