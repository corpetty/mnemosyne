import { marked } from 'marked';

export const renderMarkdown = (text: string): string => {
  return marked(text) as string;
};
