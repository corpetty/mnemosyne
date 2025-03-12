/**
 * Format a timestamp in seconds to MM:SS format
 */
export const formatTimestamp = (seconds?: number): string => {
  if (seconds === undefined || seconds === null) return '--:--';
  if (seconds < 0) seconds = 0;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.floor(seconds % 60);
  return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
};

/**
 * Format a time range with start and end timestamps
 */
export const getTimeRangeDisplay = (start?: number, end?: number): string => {
  // If we have both start and end and they're different, show a range
  if (start !== undefined && end !== undefined && start !== end) {
    return `${formatTimestamp(start)} - ${formatTimestamp(end)}`;
  }
  
  // If we only have a start time or start=end, just show that
  if (start !== undefined) {
    return formatTimestamp(start);
  }
  
  // No timestamps available
  return '--:--';
};
