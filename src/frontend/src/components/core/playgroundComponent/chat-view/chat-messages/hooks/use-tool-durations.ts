import { useEffect, useMemo, useState } from "react";
import type { ContentBlock, ToolContent } from "@/types/chat";

interface ToolItem {
  content: ToolContent;
  toolKey: string;
  blockIndex: number;
  contentIndex: number;
}

interface UseToolDurationsResult {
  toolElapsedTimes: Record<string, number>;
  totalToolDuration: number;
  allToolsCompleted: boolean;
  toolItems: ToolItem[];
}

/**
 * Hook to track tool durations from content blocks.
 * Handles both completed tools (with duration from backend) and active tools (tracked in real-time).
 */
export function useToolDurations(
  contentBlocks: ContentBlock[] | undefined,
  isLoading: boolean,
): UseToolDurationsResult {
  const [toolStartTimes, setToolStartTimes] = useState<Record<string, number>>(
    {},
  );
  const [toolElapsedTimes, setToolElapsedTimes] = useState<
    Record<string, number>
  >({});

  // Extract tool items with unique keys
  // Include blocks that have tool_use contents OR have nested_blocks
  const toolItems = useMemo(() => {
    if (!contentBlocks) return [];
    return contentBlocks.flatMap((block, blockIndex) => {
      const toolUseContents = block.contents
        .filter((content) => content.type === "tool_use")
        .map((content, contentIndex) => ({
          content,
          toolKey: `${blockIndex}-${contentIndex}`,
          blockIndex,
          contentIndex,
        }));

      // If block has nested_blocks but no tool_use contents, create a synthetic tool item
      // so the block gets rendered and nested blocks are displayed
      if (
        toolUseContents.length === 0 &&
        block.nested_blocks &&
        block.nested_blocks.length > 0
      ) {
        // Use first content item (usually text) as placeholder
        const firstContent = block.contents[0];
        if (firstContent) {
          return [
            {
              content: firstContent as ToolContent,
              toolKey: `${blockIndex}-synthetic`,
              blockIndex,
              contentIndex: 0,
            },
          ];
        }
      }

      return toolUseContents;
    });
  }, [contentBlocks]);

  // Initialize elapsed times from completed tools (for old messages or when tools first load)
  useEffect(() => {
    if (toolItems.length === 0) return;

    setToolElapsedTimes((prev) => {
      const updated: Record<string, number> = { ...prev };
      let hasChanges = false;

      toolItems.forEach(({ content, toolKey }) => {
        const toolDuration = content.duration || 0;
        // For completed tools (with duration), ensure elapsed time is set
        if (toolDuration > 0 && updated[toolKey] !== toolDuration) {
          updated[toolKey] = toolDuration;
          hasChanges = true;
        }
      });

      return hasChanges ? updated : prev;
    });
  }, [toolItems]); // Only run when toolItems change

  // Track tool start times and update elapsed times
  useEffect(() => {
    if (toolItems.length === 0) return;

    const newStartTimes: Record<string, number> = {};
    const newElapsedTimes: Record<string, number> = {};

    toolItems.forEach(({ content, toolKey }) => {
      const toolDuration = content.duration || 0;

      // If tool has no duration and is loading, track its start time
      if (isLoading && toolDuration === 0) {
        if (!toolStartTimes[toolKey]) {
          // Tool just started
          newStartTimes[toolKey] = Date.now();
        } else {
          // Tool already started, calculate elapsed time
          newElapsedTimes[toolKey] = Date.now() - toolStartTimes[toolKey];
        }
      } else if (toolDuration > 0) {
        // Tool has finished (or is from old message), use its duration
        newElapsedTimes[toolKey] = toolDuration;
      }
    });

    if (Object.keys(newStartTimes).length > 0) {
      setToolStartTimes((prev) => ({ ...prev, ...newStartTimes }));
    }
    if (Object.keys(newElapsedTimes).length > 0) {
      setToolElapsedTimes((prev) => ({ ...prev, ...newElapsedTimes }));
    }
  }, [toolItems, isLoading, toolStartTimes]);

  // Update tool elapsed times in real-time for active tools only
  useEffect(() => {
    if (!isLoading) return;

    const interval = setInterval(() => {
      setToolElapsedTimes((prev) => {
        const updated: Record<string, number> = { ...prev };
        let hasChanges = false;

        toolItems.forEach(({ content, toolKey }) => {
          const backendDuration = content.duration || 0;

          // Only update if tool is active (no backend duration yet) and we're tracking it
          // Never overwrite completed tool durations from backend
          if (backendDuration === 0 && toolStartTimes[toolKey]) {
            const elapsed = Date.now() - toolStartTimes[toolKey];
            if (updated[toolKey] !== elapsed) {
              updated[toolKey] = elapsed;
              hasChanges = true;
            }
          }
        });

        return hasChanges ? updated : prev;
      });
    }, 100);

    return () => clearInterval(interval);
  }, [isLoading, toolItems, toolStartTimes]);

  // Calculate total duration and completion status
  const { totalToolDuration, allToolsCompleted } = useMemo(() => {
    if (toolItems.length === 0) {
      return { totalToolDuration: 0, allToolsCompleted: false };
    }

    // Only use backend durations - sum all tool durations from content.duration
    // No real-time tracking, only what backend provides
    const total = toolItems.reduce((acc, { content }) => {
      const backendDuration = content.duration || 0;
      return acc + backendDuration;
    }, 0);

    const allCompleted =
      toolItems.length > 0 &&
      toolItems.every(
        (item) =>
          item.content.duration !== undefined &&
          item.content.duration !== null &&
          item.content.duration > 0,
      );

    return { totalToolDuration: total, allToolsCompleted: allCompleted };
  }, [toolItems, toolElapsedTimes]);

  return {
    toolElapsedTimes,
    totalToolDuration,
    allToolsCompleted,
    toolItems,
  };
}
