import React, { useState } from 'react';
import { renderMarkdown } from '../utils/markdown';

interface SummaryProps {
  summary: string;
  processingStatus?: string;
}

const Summary: React.FC<SummaryProps> = ({ summary, processingStatus }) => {
  const [expanded, setExpanded] = useState(true);
  
  // Handle empty or error case
  if (summary === "No transcript available to summarize.") {
    return (
      <div className="bg-gray-50 rounded-lg p-4">
        <h2 className="text-lg font-medium text-gray-900 mb-4">
          Summary
        </h2>
        <div className="text-gray-500 italic">
          No transcript available to summarize.
        </div>
      </div>
    );
  }
  
  // Check if status is related to summary generation
  const isSummaryStatus = processingStatus && (
    processingStatus.toLowerCase().includes('summary') || 
    processingStatus.toLowerCase().includes('generating')
  );
  
  return (
    <div className="bg-gray-50 rounded-lg p-4">
      <div className="flex justify-between items-center mb-4">
        <div>
          <h2 className="text-lg font-medium text-gray-900">
            Detailed Summary
          </h2>
          {isSummaryStatus && (
            <div className="text-sm text-indigo-600 mt-1 flex items-center">
              <svg className="animate-spin -ml-1 mr-2 h-4 w-4 text-indigo-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
              {processingStatus}
            </div>
          )}
        </div>
        <button 
          className="text-sm text-indigo-600 hover:text-indigo-800 focus:outline-none"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? 'Collapse' : 'Expand'}
        </button>
      </div>
      
      {expanded && (
        <div 
          className="prose prose-headings:text-indigo-700 prose-headings:font-medium 
            prose-h2:text-lg prose-h2:border-b prose-h2:border-indigo-100 prose-h2:pb-1 prose-h2:mt-5 prose-h2:mb-3
            prose-strong:text-indigo-600 prose-ul:my-2 prose-li:my-1 max-w-none
            prose-ul:pl-5 prose-ol:pl-5 prose-li:pl-0
            prose-blockquote:border-l-4 prose-blockquote:border-indigo-200 prose-blockquote:pl-4 
            prose-blockquote:italic prose-blockquote:text-gray-700 
            prose-p:my-2 prose-p:text-gray-700
            overflow-auto"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(summary) }}
        />
      )}
    </div>
  );
};

export default Summary;
