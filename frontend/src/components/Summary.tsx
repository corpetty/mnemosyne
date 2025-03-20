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
      <div className="bg-white border border-gray-200 rounded-lg shadow-sm p-5">
        <h2 className="text-lg font-semibold text-gray-800 mb-4 flex items-center">
          <svg className="h-5 w-5 mr-2 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          Summary
        </h2>
        <div className="text-gray-500 italic px-4 py-6 flex items-center justify-center">
          <svg className="h-6 w-6 mr-2 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
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
    <div className="bg-white border border-gray-200 rounded-lg shadow-sm">
      <div className="flex justify-between items-center p-5 border-b border-gray-100">
        <div>
          <h2 className="text-lg font-semibold text-gray-800 flex items-center">
            <svg className="h-5 w-5 mr-2 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
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
          className="px-3 py-1 text-sm font-medium text-indigo-600 hover:text-indigo-800 
                    border border-indigo-100 hover:border-indigo-300 rounded-md 
                    transition-colors duration-150 focus:outline-none 
                    focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? (
            <span className="flex items-center">
              <svg className="h-4 w-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
              Collapse
            </span>
          ) : (
            <span className="flex items-center">
              <svg className="h-4 w-4 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
              </svg>
              Expand
            </span>
          )}
        </button>
      </div>
      
      {expanded && (
        <div className="p-5">
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
        </div>
      )}
      
      <div className="flex justify-end p-3 bg-gray-50 rounded-b-lg border-t border-gray-100">
        <button
          className="inline-flex items-center px-3 py-1 border border-transparent text-sm 
                   font-medium rounded-md text-indigo-700 bg-indigo-100 hover:bg-indigo-200
                   focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500"
          onClick={() => {
            navigator.clipboard.writeText(summary);
          }}
        >
          <svg className="mr-1.5 -ml-1 h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3" />
          </svg>
          Copy Summary
        </button>
      </div>
    </div>
  );
};

export default Summary;
