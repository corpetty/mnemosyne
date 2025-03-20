import React, { useMemo, useState } from 'react';
import { TranscriptSegment } from '../types';
import { formatTimestamp, getTimeRangeDisplay } from '../utils/formatters';

interface TranscriptProps {
  transcript: TranscriptSegment[];
}

interface SpeakerColors {
  [key: string]: {
    text: string;
    bg: string;
  };
}

const Transcript: React.FC<TranscriptProps> = ({ transcript }) => {
  const [searchTerm, setSearchTerm] = useState('');

  // Generate consistent colors for different speakers
  const speakerColors = useMemo(() => {
    const colors: SpeakerColors = {};
    const colorOptions = [
      'text-indigo-600', 'text-teal-600', 'text-purple-600', 
      'text-rose-600', 'text-blue-600', 'text-green-600'
    ];
    const bgColors = [
      'bg-indigo-100', 'bg-teal-100', 'bg-purple-100',
      'bg-rose-100', 'bg-blue-100', 'bg-green-100'
    ];
    
    // Extract unique speakers
    const speakers = Array.from(new Set(transcript.map(segment => segment.speaker)));
    
    // Assign colors to speakers
    speakers.forEach((speaker, index) => {
      colors[speaker] = {
        text: colorOptions[index % colorOptions.length],
        bg: bgColors[index % bgColors.length]
      };
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

  // Filter for search term
  const filteredTranscript = useMemo(() => {
    if (!searchTerm.trim()) return uniqueTranscript;
    
    const term = searchTerm.toLowerCase();
    return uniqueTranscript.filter(segment => 
      segment.text.toLowerCase().includes(term) || 
      segment.speaker.toLowerCase().includes(term)
    );
  }, [uniqueTranscript, searchTerm]);
  
  const renderTranscript = () => {
    let currentSpeaker = '';
    
    return filteredTranscript.map((segment, index) => {
      // Determine if this is a new speaker
      const showSpeaker = segment.speaker !== currentSpeaker;
      currentSpeaker = segment.speaker;
      
      // Get speaker color
      const speakerStyle = speakerColors[segment.speaker] || { text: 'text-indigo-600', bg: 'bg-indigo-100' };
      
      return (
        <div 
          key={index} 
          className={`mb-4 ${showSpeaker ? 'mt-6' : ''} ${showSpeaker ? 'border-t border-gray-100 pt-4' : ''}`}
        >
          {showSpeaker && (
            <div className="flex items-center mb-2">
              <div className={`${speakerStyle.bg} ${speakerStyle.text} rounded-full px-3 py-1 text-sm font-medium`}>
                {segment.speaker}
              </div>
            </div>
          )}
          <div className="flex">
            <div className="flex-shrink-0 w-16 text-right mr-4">
              {(segment.start !== undefined && segment.end !== undefined) ? (
                <span className="text-gray-400 text-xs font-medium bg-gray-50 px-2 py-1 rounded">
                  {getTimeRangeDisplay(segment.start, segment.end)}
                </span>
              ) : (
                <span className="text-gray-300 text-xs italic">
                  --:--
                </span>
              )}
            </div>
            <div className="flex-grow">
              <p className="text-gray-700 leading-relaxed">{segment.text}</p>
            </div>
          </div>
        </div>
      );
    });
  };

  return (
    <div className="bg-white border border-gray-200 rounded-lg shadow-sm">
      <div className="flex items-center justify-between p-5 border-b border-gray-100">
        <h2 className="text-lg font-semibold text-gray-800 flex items-center">
          <svg className="h-5 w-5 mr-2 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
          </svg>
          Transcript
        </h2>
        
        <div className="relative">
          <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
            <svg className="h-4 w-4 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          </div>
          <input
            type="text"
            placeholder="Search transcript..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="pl-10 pr-4 py-1.5 block w-full border border-gray-300 rounded-md text-sm focus:ring-indigo-500 focus:border-indigo-500"
          />
          {searchTerm && (
            <button
              onClick={() => setSearchTerm('')}
              className="absolute inset-y-0 right-0 pr-3 flex items-center"
            >
              <svg className="h-4 w-4 text-gray-400 hover:text-gray-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>
      </div>
      
      <div className="p-5">
        {filteredTranscript.length === 0 ? (
          <div className="text-center py-10">
            {transcript.length === 0 ? (
              <div className="space-y-3">
                <svg className="mx-auto h-12 w-12 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                </svg>
                <p className="text-gray-500">No transcript segments available yet.</p>
              </div>
            ) : (
              <div className="space-y-3">
                <svg className="mx-auto h-12 w-12 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                <p className="text-gray-500">No matches found for "{searchTerm}"</p>
                <button 
                  onClick={() => setSearchTerm('')}
                  className="text-indigo-600 hover:text-indigo-800 text-sm font-medium"
                >
                  Clear search
                </button>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-1 overflow-auto max-h-96">
            {renderTranscript()}
          </div>
        )}
      </div>
      
      {filteredTranscript.length > 0 && (
        <div className="bg-gray-50 px-5 py-3 border-t border-gray-100 text-xs text-gray-500 flex justify-between items-center rounded-b-lg">
          <span>
            {searchTerm 
              ? `${filteredTranscript.length} of ${uniqueTranscript.length} segments`
              : `${uniqueTranscript.length} transcript segments`
            }
          </span>
          
          <div className="flex space-x-2">
            <div className="flex items-center">
              <span className="inline-flex h-2 w-2 rounded-full bg-blue-400 mr-1"></span>
              <span>Current speaker</span>
            </div>
            <div className="flex items-center">
              <span className="inline-flex h-2 w-2 rounded-full bg-red-400 mr-1"></span>
              <span>Recording</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Transcript;
