import React from 'react';
import { renderMarkdown } from '../utils/markdown';

interface SummaryProps {
  summary: string;
}

const Summary: React.FC<SummaryProps> = ({ summary }) => {
  return (
    <div className="bg-gray-50 rounded-lg p-4">
      <h2 className="text-lg font-medium text-gray-900 mb-4">
        Summary
      </h2>
      {summary === "No transcript available to summarize." ? (
        <div className="text-gray-500 italic">
          No transcript available to summarize.
        </div>
      ) : (
        <div
          className="prose prose-headings:text-indigo-700 prose-headings:font-medium prose-headings:text-base 
          prose-h2:border-b prose-h2:border-indigo-100 prose-h2:pb-1 prose-h2:mt-4 prose-h2:mb-3
          prose-strong:text-indigo-600 prose-ul:my-2 prose-li:my-1 max-w-none"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(summary) }}
        />
      )}
    </div>
  );
};

export default Summary;
