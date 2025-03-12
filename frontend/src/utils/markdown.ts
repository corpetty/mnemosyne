import { marked } from 'marked';

export const renderMarkdown = (text: string): string => {
  // Configure marked options for better handling of detailed markdown
  marked.setOptions({
    breaks: true,         // Add line breaks
    gfm: true,            // Enable GitHub flavored markdown
    headerIds: true,      // Generate IDs for headers
    pedantic: false,      // Don't be overly precise
    sanitize: false       // Don't sanitize HTML (React will handle this)
  });
  
  return marked(text) as string;
};
