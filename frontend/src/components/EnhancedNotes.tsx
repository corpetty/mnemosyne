import React, { useState } from 'react';
import { renderMarkdown } from '../utils/markdown';
import { AudioDevice, Model } from '../types';

interface EnhancedNotesProps {
  notes: string;
  sessionId: string;
  onSave: (sessionId: string, notes: string) => Promise<boolean>;
  isSaving?: boolean;
  isRecording: boolean;
  isProcessing: boolean;
  devices: AudioDevice[];
  selectedDevices: string[];
  models: Model[];
  selectedModel: string;
  processingStatus: string;
  onDeviceToggle: (deviceId: string) => void;
  onStartRecording: () => Promise<void>;
  onStopRecording: () => Promise<void>;
  onModelChange: (modelId: string) => void;
}

const EnhancedNotes: React.FC<EnhancedNotesProps> = ({
  notes,
  sessionId,
  onSave,
  isSaving = false,
  isRecording,
  isProcessing,
  devices,
  selectedDevices,
  models,
  selectedModel,
  processingStatus,
  onDeviceToggle,
  onStartRecording,
  onStopRecording,
  onModelChange,
}) => {
  const [editMode, setEditMode] = useState(false);
  const [editedNotes, setEditedNotes] = useState(notes);
  const [localSaving, setLocalSaving] = useState(false);
  const [showDeviceSelection, setShowDeviceSelection] = useState(false);
  
  // Switch to edit mode, preparing the notes for editing
  const handleEdit = () => {
    setEditedNotes(notes);
    setEditMode(true);
  };
  
  // Cancel editing and revert to the original notes
  const handleCancel = () => {
    setEditedNotes(notes);
    setEditMode(false);
  };
  
  // Save the edited notes
  const handleSave = async () => {
    if (isSaving || localSaving) return;
    
    setLocalSaving(true);
    try {
      const success = await onSave(sessionId, editedNotes);
      if (success) {
        setEditMode(false);
      }
    } finally {
      setLocalSaving(false);
    }
  };
  
  const saving = isSaving || localSaving;
  
  // Empty state when no notes exist
  const emptyState = !editMode && (!notes || notes.trim() === '');
  
  // Toggle device selection panel
  const toggleDeviceSelection = () => {
    setShowDeviceSelection(!showDeviceSelection);
  };

  return (
    <div className="bg-white border border-gray-200 rounded-lg shadow-sm">
      {/* Recording Controls Header */}
      <div className="p-4 bg-gray-50 border-b border-gray-200 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center">
            <h2 className="text-lg font-semibold text-gray-800">Meeting Notes</h2>
            {isRecording && (
              <span className="ml-3 inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800">
                <span className="mr-1 h-2 w-2 rounded-full bg-red-500 animate-pulse"></span>
                Recording
              </span>
            )}
            {isProcessing && (
              <span className="ml-3 inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800">
                <svg className="animate-spin -ml-0.5 mr-1.5 h-2 w-2 text-yellow-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Processing
              </span>
            )}
          </div>

          <div className="flex space-x-2">
            <button
              onClick={toggleDeviceSelection}
              className="px-3 py-1 text-sm border border-gray-300 rounded-md hover:bg-gray-50"
              disabled={isRecording}
            >
              <span className="flex items-center">
                <svg className="h-4 w-4 mr-1" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" />
                </svg>
                Devices {selectedDevices.length > 0 && `(${selectedDevices.length})`}
              </span>
            </button>
            
            <button
              onClick={isRecording ? onStopRecording : onStartRecording}
              disabled={selectedDevices.length === 0 || isProcessing}
              className={`flex items-center px-3 py-1 text-sm text-white rounded-md ${
                selectedDevices.length === 0 || isProcessing
                  ? 'bg-gray-400 cursor-not-allowed'
                  : isRecording
                  ? 'bg-red-600 hover:bg-red-700'
                  : 'bg-green-600 hover:bg-green-700'
              }`}
            >
              {isRecording ? (
                <>
                  <span className="mr-1">⏹</span>
                  Stop
                </>
              ) : (
                <>
                  <span className="mr-1">⏵</span>
                  Record
                </>
              )}
            </button>
            
            {!emptyState && !editMode && (
              <button 
                onClick={handleEdit}
                className="px-3 py-1 text-sm font-medium text-indigo-600 hover:text-indigo-800 
                          border border-indigo-100 hover:border-indigo-300 rounded-md 
                          transition-colors duration-150 focus:outline-none 
                          focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2"
              >
                <span className="flex items-center">
                  <svg className="h-4 w-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a1 1 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                  </svg>
                  Edit
                </span>
              </button>
            )}
          </div>
        </div>
        
        {/* Expandable device selection */}
        {showDeviceSelection && (
          <div className="mt-3 p-3 bg-white border border-gray-200 rounded-md shadow-sm">
            <div className="flex justify-between mb-2">
              <h3 className="text-sm font-medium text-gray-700">Audio Sources</h3>
              <select
                value={selectedModel}
                onChange={(e) => onModelChange(e.target.value)}
                disabled={isRecording || isProcessing}
                className="text-xs border-gray-300 rounded-md focus:ring-indigo-500 focus:border-indigo-500"
              >
                <option value="">Default Model</option>
                {models.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name}
                  </option>
                ))}
              </select>
            </div>
            
            <div className="grid grid-cols-2 gap-2">
              {devices.length > 0 && devices.some(d => d.is_monitor) && (
                <div className="space-y-1">
                  <h4 className="text-xs font-medium text-gray-500">System Audio</h4>
                  {devices
                    .filter(d => d.is_monitor)
                    .map((device) => (
                      <label key={device.id} className="flex items-center space-x-2">
                        <input
                          type="checkbox"
                          checked={selectedDevices.includes(String(device.id))}
                          onChange={() => onDeviceToggle(String(device.id))}
                          disabled={isRecording || isProcessing}
                          className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                        />
                        <span className="text-xs text-gray-700">{device.name}</span>
                      </label>
                    ))}
                </div>
              )}
              <div className="space-y-1">
                <h4 className="text-xs font-medium text-gray-500">Input Devices</h4>
                {devices
                  .filter(d => !d.is_monitor)
                  .map((device) => (
                    <label key={device.id} className="flex items-center space-x-2">
                      <input
                        type="checkbox"
                        checked={selectedDevices.includes(String(device.id))}
                        onChange={() => onDeviceToggle(String(device.id))}
                        disabled={isRecording || isProcessing}
                        className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                      />
                      <span className="text-xs text-gray-700">
                        {device.name} {device.default ? '(Default)' : ''}
                      </span>
                    </label>
                  ))}
              </div>
            </div>
          </div>
        )}
        
        {processingStatus && (
          <div className="text-sm text-gray-600">
            {processingStatus}
          </div>
        )}
      </div>
      
      {/* Notes Content */}
      <div className="p-5">
        {editMode ? (
          <div className="space-y-4">
            <textarea
              value={editedNotes}
              onChange={(e) => setEditedNotes(e.target.value)}
              className="w-full h-64 p-3 border border-gray-300 rounded-md focus:ring-indigo-500 focus:border-indigo-500"
              placeholder="Add your notes in markdown format..."
            />
            
            <div className="flex justify-end space-x-3">
              <button
                onClick={handleCancel}
                disabled={saving}
                className="px-4 py-2 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
              >
                Cancel
              </button>
              
              <button
                onClick={handleSave}
                disabled={saving}
                className="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
              >
                {saving ? (
                  <>
                    <svg className="animate-spin -ml-1 mr-2 h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    Saving...
                  </>
                ) : 'Save Notes'}
              </button>
            </div>
          </div>
        ) : emptyState ? (
          <div className="text-center py-12">
            <svg className="mx-auto h-12 w-12 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a1 1 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
            </svg>
            <p className="mt-2 text-sm text-gray-500">No notes yet.</p>
            <button
              onClick={handleEdit}
              className="mt-3 inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
            >
              <svg className="-ml-1 mr-2 h-5 w-5 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              Add Notes
            </button>
          </div>
        ) : (
          <div 
            className="prose prose-indigo prose-headings:text-indigo-700 prose-headings:font-medium 
              prose-h2:text-lg prose-h2:border-b prose-h2:border-indigo-100 prose-h2:pb-1 prose-h2:mt-5 prose-h2:mb-3
              prose-strong:text-indigo-600 prose-ul:my-2 prose-li:my-1 max-w-none
              prose-ul:pl-5 prose-ol:pl-5 prose-li:pl-0
              prose-blockquote:border-l-4 prose-blockquote:border-indigo-200 prose-blockquote:pl-4 
              prose-blockquote:italic prose-blockquote:text-gray-700 
              prose-p:my-2 prose-p:text-gray-700
              overflow-auto"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(notes) }}
          />
        )}
      </div>
      
      {!editMode && !emptyState && (
        <div className="bg-gray-50 px-5 py-3 border-t border-gray-100 text-xs text-gray-500 flex justify-between items-center rounded-b-lg">
          <span>
            Markdown formatting supported
          </span>
          
          <div className="flex space-x-2">
            <div className="flex items-center">
              <span className="inline-flex h-2 w-2 rounded-full bg-indigo-400 mr-1"></span>
              <span>Heading</span>
            </div>
            <div className="flex items-center">
              <span className="inline-flex h-2 w-2 rounded-full bg-green-400 mr-1"></span>
              <span>List</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default EnhancedNotes;
