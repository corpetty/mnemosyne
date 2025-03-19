import React, { useState } from 'react';
import { Participant } from '../types';

interface ParticipantListProps {
  participants: Participant[];
  sessionId: string;
  onParticipantsUpdated?: () => void;
}

interface RenameFormProps {
  participantId: string;
  currentName: string;
  onRename: (participantId: string, newName: string) => void;
  onCancel: () => void;
}

const RenameForm: React.FC<RenameFormProps> = ({ participantId, currentName, onRename, onCancel }) => {
  const [newName, setNewName] = useState(currentName);
  
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (newName.trim()) {
      onRename(participantId, newName.trim());
    }
  };
  
  return (
    <form onSubmit={handleSubmit} className="flex items-center mt-1 mb-2">
      <input
        type="text"
        value={newName}
        onChange={(e) => setNewName(e.target.value)}
        className="flex-1 px-2 py-1 border border-gray-300 rounded mr-2"
        placeholder="Enter participant name"
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

const ParticipantList: React.FC<ParticipantListProps> = ({ participants, sessionId, onParticipantsUpdated }) => {
  const [renamingParticipantId, setRenamingParticipantId] = useState<string | null>(null);
  const [isExtracting, setIsExtracting] = useState(false);
  
  // Function to update participant name
  const handleRename = async (participantId: string, newName: string) => {
    try {
      const response = await fetch(`/sessions/${sessionId}/participants/${participantId}`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ participant_id: participantId, name: newName }),
      });
      
      if (!response.ok) {
        throw new Error('Failed to rename participant');
      }
      
      // Reset rename state
      setRenamingParticipantId(null);
      
      // Notify parent component that participants were updated
      if (onParticipantsUpdated) {
        onParticipantsUpdated();
      }
    } catch (error) {
      console.error('Error renaming participant:', error);
    }
  };

  // Function to extract participants from transcript
  const extractParticipants = async () => {
    setIsExtracting(true);
    try {
      const response = await fetch(`/sessions/${sessionId}/participants/extract`, {
        method: 'POST',
      });
      
      if (!response.ok) {
        throw new Error('Failed to extract participants');
      }
      
      // Notify parent component that participants were updated
      if (onParticipantsUpdated) {
        onParticipantsUpdated();
      }
    } catch (error) {
      console.error('Error extracting participants:', error);
    } finally {
      setIsExtracting(false);
    }
  };

  return (
    <div className="bg-white shadow rounded-lg p-4 my-4">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-lg font-medium text-gray-900">Participants</h2>
        {participants.length === 0 && (
          <button
            onClick={extractParticipants}
            disabled={isExtracting}
            className="px-3 py-1 bg-blue-500 text-white text-sm rounded hover:bg-blue-600 disabled:bg-blue-300"
          >
            {isExtracting ? 'Extracting...' : 'Extract Participants'}
          </button>
        )}
      </div>

      {participants.length === 0 ? (
        <div className="text-gray-500 text-center py-4">
          No participants identified yet. Extract them from the transcript.
        </div>
      ) : (
        <ul className="divide-y divide-gray-200">
          {participants.map((participant) => (
            <li key={participant.id} className="py-3">
              {renamingParticipantId === participant.id ? (
                <RenameForm
                  participantId={participant.id}
                  currentName={participant.name}
                  onRename={handleRename}
                  onCancel={() => setRenamingParticipantId(null)}
                />
              ) : (
                <div className="flex justify-between items-center">
                  <div className="flex items-center">
                    <div className="font-medium">{participant.name}</div>
                    {participant.id !== participant.name && (
                      <div className="text-gray-500 text-sm ml-2">({participant.id})</div>
                    )}
                  </div>
                  <button
                    onClick={() => setRenamingParticipantId(participant.id)}
                    className="text-blue-500 hover:text-blue-700 text-sm"
                  >
                    Rename
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

export default ParticipantList;
