import React, { useMemo } from 'react';
import { TranscriptSegment } from '../types';
import { formatTimestamp, getTimeRangeDisplay } from '../utils/formatters';

interface TranscriptProps {
  transcript: TranscriptSegment[];
}

interface SpeakerColors {
  [key: string]: string;
}

const Transcript: React.FC<TranscriptProps> = ({ transcript }) => {
  // Generate consistent colors for different speakers
  const speakerColors = useMemo(() => {
    const colors: SpeakerColors = {};
    const colorOptions = [
      'text-indigo-600', 'text-teal-600', 'text-purple-600', 
      'text-rose-600', 'text-blue-600', 'text-green-600'
    ];
    
    // Extract unique speakers
    const speakers = Array.from(new Set(transcript.map(segment => segment.speaker)));
    
    // Assign colors to speakers
    speakers.forEach((speaker, index) => {
      colors[speaker] = colorOptions[index % colorOptions.length];
    });
    
    return colors;
  }, [transcript]);
  
  // Filter out duplicate segments (same text from same speaker)
  const uniqueTranscript = useMemo(() => {
    const seen = new Set();
    return transcript.filter(segment => {
      const key = `${segment.speaker}:${segment.text}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [transcript]);
  
  const renderTranscript = () => {
    let currentSpeaker = '';
    
    return uniqueTranscript.map((segment, index) => {
      // Determine if this is a new speaker
      const showSpeaker = segment.speaker !== currentSpeaker;
      currentSpeaker = segment.speaker;
      
      // Get speaker color
      const speakerColor = speakerColors[segment.speaker] || 'text-indigo-600';
      
      return (
        <div 
          key={index} 
          className={`mb-3 ${showSpeaker ? 'mt-5' : ''} pb-2 ${showSpeaker ? 'border-t border-gray-200 pt-2' : ''}`}
        >
          {showSpeaker && (
            <div className={`font-bold ${speakerColor} mb-1`}>
              {segment.speaker}
            </div>
          )}
          <div className="flex items-start">
            {(segment.start !== undefined && segment.end !== undefined) ? (
              <span className="text-gray-500 text-sm mr-3 mt-1 whitespace-nowrap min-w-[60px]">
                {getTimeRangeDisplay(segment.start, segment.end)}
              </span>
            ) : (
              <span className="text-gray-400 text-sm mr-3 mt-1 italic">
                --:--
              </span>
            )}
            <span className="text-gray-700">{segment.text}</span>
          </div>
        </div>
      );
    });
  };

  return (
    <div className="bg-gray-50 rounded-lg p-4 shadow-sm">
      <h2 className="text-lg font-medium text-gray-900 mb-4 flex items-center">
        <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 mr-2 text-indigo-500" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M10 2a8 8 0 100 16 8 8 0 000-16zm0 14a6 6 0 110-12 6 6 0 010 12z" clipRule="evenodd" />
          <path fillRule="evenodd" d="M10 5a1 1 0 011 1v3.586l2.707 2.707a1 1 0 01-1.414 1.414l-3-3A1 1 0 019 10V6a1 1 0 011-1z" clipRule="evenodd" />
        </svg>
        Transcript
      </h2>
      
      {uniqueTranscript.length === 0 ? (
        <div className="text-gray-500 italic py-4 text-center">
          No transcript segments available yet.
        </div>
      ) : (
        <div className="space-y-1">
          {renderTranscript()}
        </div>
      )}
    </div>
  );
};

export default Transcript;
