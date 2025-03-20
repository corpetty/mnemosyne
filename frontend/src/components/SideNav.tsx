import React from 'react';

interface NavItemProps {
  icon: string;
  label: string;
  active?: boolean;
  onClick: () => void;
}

const NavItem: React.FC<NavItemProps> = ({ icon, label, active, onClick }) => {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center px-4 py-3 text-sm font-medium ${
        active
          ? 'text-indigo-600 bg-indigo-50 border-l-4 border-indigo-600'
          : 'text-gray-600 hover:text-indigo-600 hover:bg-gray-50'
      }`}
    >
      <svg
        className={`mr-3 h-5 w-5 ${active ? 'text-indigo-500' : 'text-gray-400 group-hover:text-indigo-500'}`}
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d={icon}
        />
      </svg>
      {label}
    </button>
  );
};

interface SideNavProps {
  activeSession: string | null;
  sessions: Array<{
    session_id: string;
    name?: string;
    status: string;
  }>;
  onSessionSelect: (sessionId: string) => void;
  onCreateSession: () => void;
}

const SideNav: React.FC<SideNavProps> = ({
  activeSession,
  sessions,
  onSessionSelect,
  onCreateSession
}) => {
  // Icon paths
  const sessionIcon = 'M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z';
  const micIcon = 'M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z';
  const uploadIcon = 'M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12';
  const settingsIcon = 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z';

  // Get a displayable name for a session
  const getSessionName = (session: { session_id: string; name?: string }, index: number) => {
    const shortId = session.session_id.substring(0, 6);
    if (session.name) {
      return `${session.name} (${shortId})`;
    }
    return `Session ${index + 1} (${shortId})`;
  };

  return (
    <div className="flex flex-col h-full">
      <div className="p-4 border-b border-gray-200">
        <button
          onClick={onCreateSession}
          className="w-full flex justify-center items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
        >
          <svg className="mr-2 -ml-1 h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
          </svg>
          New Session
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="py-2">
          <h3 className="px-4 text-xs font-semibold text-gray-500 uppercase tracking-wider">
            Main Navigation
          </h3>
          <div className="mt-1">
            <NavItem
              icon={sessionIcon}
              label="All Sessions"
              active={!activeSession}
              onClick={() => onSessionSelect('')}
            />
            <NavItem
              icon={micIcon}
              label="Record"
              onClick={() => {}}
            />
            <NavItem
              icon={uploadIcon}
              label="Upload"
              onClick={() => {}}
            />
            <NavItem
              icon={settingsIcon}
              label="Settings"
              onClick={() => {}}
            />
          </div>
        </div>

        {sessions.length > 0 && (
          <div className="py-2">
            <h3 className="px-4 text-xs font-semibold text-gray-500 uppercase tracking-wider">
              Recent Sessions
            </h3>
            <div className="mt-1">
              {sessions.slice(0, 5).map((session, index) => (
                <NavItem
                  key={session.session_id}
                  icon={sessionIcon}
                  label={getSessionName(session, index)}
                  active={activeSession === session.session_id}
                  onClick={() => onSessionSelect(session.session_id)}
                />
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="p-4 border-t border-gray-200">
        <div className="flex items-center">
          <div className="flex-shrink-0">
            <svg className="h-8 w-8 text-indigo-500" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" 
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M12 11c1.105 0 2-.895 2-2s-.895-2-2-2-2 .895-2 2 .895 2 2 2zM12 15c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4z" 
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="ml-3">
            <p className="text-sm font-medium text-gray-700">Mnemosyne</p>
            <p className="text-xs text-gray-500">Memory Enhancement System</p>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SideNav;
