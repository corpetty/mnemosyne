import React, { ReactNode } from 'react';

interface LayoutProps {
  children: ReactNode;
  sidebar?: ReactNode;
  className?: string;
}

const Layout: React.FC<LayoutProps> = ({ children, className = '' }) => {
  return (
    <div className={`min-h-screen bg-gray-50 ${className}`}>
      <div className="max-w-7xl mx-auto py-6 px-4 sm:px-6 lg:px-8">
        <main className="flex-1">
          <div className="bg-white rounded-lg shadow-md p-6">
            {children}
          </div>
        </main>
      </div>
    </div>
  );
};

export default Layout;
