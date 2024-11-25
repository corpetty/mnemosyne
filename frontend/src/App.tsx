import React, { useState, useEffect } from 'react';
import { marked } from 'marked';

interface TranscriptSegment {
  text: string;
  timestamp: number;
  speaker: string;
  start?: number;
  end?: number;
}

interface AudioDevice {
  id: string | number;
  name: string;
  channels: number;
  default: boolean;
  is_monitor?: boolean;
}

function App() {
  const [isRecording, setIsRecording] = useState(false);
  const [transcript, setTranscript] = useState<TranscriptSegment[]>([]);
  const [summary, setSummary] = useState<string>('');
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [selectedDevices, setSelectedDevices] = useState<Array<string>>([]);

  // Fetch available audio devices
  useEffect(() => {
    const fetchDevices = async () => {
      try {
        const response = await fetch('http://localhost:8000/devices');
        const data = await response.json();
        console.log('Available devices:', data.devices);
        setDevices(data.devices);
        
        // First try to find a monitor source
        const monitorDevice = data.devices.find((d: AudioDevice) => d.is_monitor);
        if (monitorDevice) {
          console.log('Found monitor device:', monitorDevice);
          setSelectedDevices([String(monitorDevice.id)]);
        } else {
          // Fall back to default input device
          const defaultDevice = data.devices.find((d: AudioDevice) => d.default);
          if (defaultDevice) {
            console.log('Found default device:', defaultDevice);
            setSelectedDevices([String(defaultDevice.id)]);
          }
        }
      } catch (error) {
        console.error('Error fetching devices:', error);
      }
    };
    fetchDevices();
  }, []);

  // Initialize WebSocket connection
  useEffect(() => {
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
      }
    };

    websocket.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    return () => {
      websocket.close();
    };
  }, []);

  const startRecording = async () => {
    try {
      console.log('Starting recording with devices:', selectedDevices);
      const response = await fetch('http://localhost:8000/start', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          device_ids: selectedDevices
        }),
      });
      if (response.ok) {
        setIsRecording(true);
        setTranscript([]);
        setSummary('');
      }
    } catch (error) {
      console.error('Error starting recording:', error);
    }
  };

  const stopRecording = async () => {
    try {
      const response = await fetch('http://localhost:8000/stop', {
        method: 'POST',
      });
      const data = await response.json();
      if (response.ok) {
        setIsRecording(false);
        setSummary(data.summary);
      }
    } catch (error) {
      console.error('Error stopping recording:', error);
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

  const renderMarkdown = (text: string): string => {
    return marked(text) as string;
  };

  const formatTimestamp = (seconds?: number): string => {
    if (seconds === undefined) return '';
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.floor(seconds % 60);
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
  };

  const renderTranscript = () => {
    let currentSpeaker = '';
    return transcript.map((segment, index) => {
      const showSpeaker = segment.speaker !== currentSpeaker;
      currentSpeaker = segment.speaker;
      
      return (
        <div key={index} className={`mb-2 ${showSpeaker ? 'mt-4' : ''}`}>
          {showSpeaker && (
            <div className="font-bold text-indigo-600 mb-1">
              {segment.speaker}
            </div>
          )}
          <div className="flex items-start">
            {segment.start !== undefined && (
              <span className="text-gray-500 text-sm mr-2 mt-1">
                {formatTimestamp(segment.start)}
              </span>
            )}
            <span className="text-gray-700">{segment.text}</span>
          </div>
        </div>
      );
    });
  };

  return (
    <div className="min-h-screen bg-gray-100">
      <div className="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
        <div className="px-4 py-6 sm:px-0">
          <div className="bg-white shadow rounded-lg p-6">
            <div className="flex justify-between items-center mb-6">
              <h1 className="text-2xl font-bold text-gray-900">
                Audio Transcriber
              </h1>
              <div className="flex flex-col space-y-4">
                <div className="flex flex-col space-y-2">
                  <h3 className="text-sm font-medium text-gray-700">Select Audio Sources:</h3>
                  {devices.some(d => d.is_monitor) && (
                    <div className="space-y-2">
                      <h4 className="text-xs font-medium text-gray-500">System Audio</h4>
                      {devices
                        .filter(d => d.is_monitor)
                        .map((device) => (
                          <label key={device.id} className="flex items-center space-x-2">
                            <input
                              type="checkbox"
                              checked={selectedDevices.includes(String(device.id))}
                              onChange={() => handleDeviceToggle(String(device.id))}
                              disabled={isRecording}
                              className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                            />
                            <span className="text-sm text-gray-700">{device.name}</span>
                          </label>
                        ))}
                    </div>
                  )}
                  <div className="space-y-2">
                    <h4 className="text-xs font-medium text-gray-500">Input Devices</h4>
                    {devices
                      .filter(d => !d.is_monitor)
                      .map((device) => (
                        <label key={device.id} className="flex items-center space-x-2">
                          <input
                            type="checkbox"
                            checked={selectedDevices.includes(String(device.id))}
                            onChange={() => handleDeviceToggle(String(device.id))}
                            disabled={isRecording}
                            className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                          />
                          <span className="text-sm text-gray-700">
                            {device.name} {device.default ? '(Default)' : ''}
                          </span>
                        </label>
                      ))}
                  </div>
                </div>
                <button
                  onClick={isRecording ? stopRecording : startRecording}
                  disabled={selectedDevices.length === 0}
                  className={`inline-flex items-center px-4 py-2 border border-transparent rounded-md shadow-sm text-sm font-medium text-white ${
                    selectedDevices.length === 0
                      ? 'bg-gray-400 cursor-not-allowed'
                      : isRecording
                      ? 'bg-red-600 hover:bg-red-700'
                      : 'bg-green-600 hover:bg-green-700'
                  }`}
                >
                  {isRecording ? (
                    <>
                      <span className="mr-2">⏹</span>
                      Stop Recording
                    </>
                  ) : (
                    <>
                      <span className="mr-2">⏵</span>
                      Start Recording
                    </>
                  )}
                </button>
              </div>
            </div>

            <div className="space-y-6">
              <div className="bg-gray-50 rounded-lg p-4">
                <h2 className="text-lg font-medium text-gray-900 mb-4">
                  Live Transcript
                </h2>
                <div className="space-y-2">
                  {renderTranscript()}
                </div>
              </div>

              {summary && (
                <div className="bg-gray-50 rounded-lg p-4">
                  <h2 className="text-lg font-medium text-gray-900 mb-4">
                    Summary
                  </h2>
                  <div
                    className="prose"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(summary) }}
                  />
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
