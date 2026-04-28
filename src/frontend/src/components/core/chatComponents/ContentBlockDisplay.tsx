"use client";
// Cache bust: v2024-04-21-filter-fix
import { motion } from "framer-motion";
import { ChevronDown } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { BorderTrail } from "@/components/core/border-trail";
import { useToolDurations } from "@/components/core/playgroundComponent/chat-view/chat-messages/hooks/use-tool-durations";
import {
  formatTime,
  formatToolTitle,
} from "@/components/core/playgroundComponent/chat-view/chat-messages/utils/format";
import type { ContentBlock, ContentType } from "@/types/chat";
import { cn } from "@/utils/utils";
import ForwardedIconComponent from "../../common/genericIconComponent";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "../../ui/accordion";
import ContentDisplay from "./ContentDisplay";
import DurationDisplay from "./DurationDisplay";

interface ContentBlockDisplayProps {
  contentBlocks: ContentBlock[];
  isLoading?: boolean;
  state?: string;
  chatId: string;
  playgroundPage?: boolean;
  hideHeader?: boolean;
}

// Recursive component for rendering nested blocks
function NestedBlockRenderer({
  block,
  blockIndex,
  toolKey,
  toolElapsedTimes,
  chatId,
  playgroundPage,
  depth = 0,
}: {
  block: ContentBlock;
  blockIndex: number;
  toolKey: string;
  toolElapsedTimes: Record<string, number>;
  chatId: string;
  playgroundPage?: boolean;
  depth?: number;
}) {
  const [isExpanded, setIsExpanded] = useState(block.is_expanded ?? false);

  // DEBUG: Log block information
  console.log("[NESTED BLOCKS DEBUG] NestedBlockRenderer called:", {
    blockTitle: block.title,
    blockType: block.block_type,
    depth,
    hasNestedBlocks: !!(block.nested_blocks && block.nested_blocks.length > 0),
    nestedBlocksCount: block.nested_blocks?.length || 0,
    nestedBlockTitles: block.nested_blocks?.map((b) => b.title) || [],
    toolKey,
    blockIndex,
  });

  // Process nested blocks recursively
  const nestedToolItems = useMemo(() => {
    if (!block.nested_blocks || block.nested_blocks.length === 0) {
      console.log(
        "[NESTED BLOCKS DEBUG] No nested blocks to process for:",
        block.title,
      );
      return [];
    }

    console.log("[NESTED BLOCKS DEBUG] Processing nested blocks:", {
      parentTitle: block.title,
      nestedCount: block.nested_blocks.length,
      nestedBlocks: block.nested_blocks.map((nb) => ({
        title: nb.title,
        blockType: nb.block_type,
        contentsCount: nb.contents.length,
        contentTypes: nb.contents.map((c) => c.type),
      })),
    });

    const items = block.nested_blocks.flatMap((nestedBlock, nestedIndex) => {
      console.log("[NESTED BLOCK PROCESSING] Processing nested block:", {
        nestedIndex,
        nestedBlockTitle: nestedBlock.title,
        nestedBlockType: nestedBlock.block_type,
        contentsCount: nestedBlock.contents.length,
        contents: nestedBlock.contents.map((c, idx) => ({
          index: idx,
          type: c.type,
          name: c.type === "tool_use" ? c.name : "N/A",
        })),
      });

      const toolUseContents = nestedBlock.contents
        .filter((content) => {
          const isToolUse = content.type === "tool_use";
          console.log(
            "[NESTED BLOCK PROCESSING] Examining content for tool_use:",
            {
              nestedBlockTitle: nestedBlock.title,
              contentType: content.type,
              isToolUse,
              toolName: isToolUse ? content.name : "N/A",
            },
          );
          return isToolUse;
        })
        .map((content, contentIndex) => {
          console.log(
            "[NESTED BLOCK PROCESSING] Creating tool item from nested block content:",
            {
              nestedBlockTitle: nestedBlock.title,
              toolName: content.name,
              contentIndex,
              toolKey: `${toolKey}-nested-${nestedIndex}-${contentIndex}`,
            },
          );
          return {
            content,
            toolKey: `${toolKey}-nested-${nestedIndex}-${contentIndex}`,
            blockIndex: nestedIndex,
            contentIndex,
            nestedBlock,
          };
        });

      console.log("[NESTED BLOCK PROCESSING] Tool use contents extracted:", {
        nestedBlockTitle: nestedBlock.title,
        toolUseCount: toolUseContents.length,
        toolNames: toolUseContents.map((t) => t.content.name),
      });

      // If nested block has no tool_use contents but has content or further nested blocks,
      // create a synthetic item so it gets rendered
      if (toolUseContents.length === 0 && nestedBlock.contents.length > 0) {
        const firstContent = nestedBlock.contents[0];
        return [
          {
            content: firstContent as ContentType,
            toolKey: `${toolKey}-nested-${nestedIndex}-synthetic`,
            blockIndex: nestedIndex,
            contentIndex: 0,
            nestedBlock,
          },
        ];
      }

      return toolUseContents;
    });

    console.log("[NESTED BLOCKS DEBUG] Processed nested tool items:", {
      parentTitle: block.title,
      itemsCount: items.length,
      items: items.map((i) => ({
        toolKey: i.toolKey,
        nestedBlockTitle: i.nestedBlock.title,
      })),
    });

    return items;
  }, [block.nested_blocks, toolKey, block.title]);

  const hasNestedBlocks = block.nested_blocks && block.nested_blocks.length > 0;
  const indentClass = depth > 0 ? `ml-${Math.min(depth * 4, 12)}` : "";

  console.log("[NESTED BLOCKS DEBUG] Render decision:", {
    blockTitle: block.title,
    hasNestedBlocks,
    nestedToolItemsCount: nestedToolItems.length,
    willRenderNestedSection: hasNestedBlocks && nestedToolItems.length > 0,
  });

  return (
    <div className={cn("relative", indentClass)}>
      {block.contents
        .filter((content, contentIndex) => {
          // Log EVERY content item that comes through
          console.log("[ContentBlockDisplay] Processing content item:", {
            blockTitle: block.title,
            contentIndex,
            contentType: content.type,
            contentName: content.type === "tool_use" ? content.name : "N/A",
            hasHeader: !!content.header,
            headerTitle: content.header?.title || "N/A",
          });

          // Filter out INPUT and OUTPUT tool calls by name
          if (content.type === "tool_use") {
            const toolName = content.name?.toUpperCase() || "";
            const shouldFilter = toolName === "INPUT" || toolName === "OUTPUT";
            console.log("[ContentBlockDisplay] tool_use decision:", {
              name: content.name,
              toolName,
              shouldFilter,
              willKeep: !shouldFilter,
            });
            if (shouldFilter) {
              return false;
            }
            return true;
          }
          // Also accept text content for nested notifications
          if (content.type === "text") {
            console.log("[ContentBlockDisplay] Keeping text content");
            return true;
          }
          console.log(
            "[ContentBlockDisplay] Filtering out non-tool_use/non-text content",
          );
          return false;
        })
        .map((content, contentIndex) => {
          const currentToolKey = `${toolKey}-${contentIndex}`;
          const rawTitle =
            content.header?.title ||
            (content.type === "tool_use" ? content.name : block.title) ||
            `Tool ${contentIndex + 1}`;
          const toolTitle =
            typeof rawTitle === "string" ? formatToolTitle(rawTitle) : rawTitle;
          const toolDuration =
            toolElapsedTimes[currentToolKey] ?? content.duration ?? 0;

          return (
            <div key={currentToolKey} className="mb-2">
              <AccordionItem
                value={currentToolKey}
                className="border border-border rounded-lg overflow-hidden bg-background"
              >
                <AccordionTrigger className="hover:bg-muted hover:no-underline px-3 py-2.5">
                  <div className="flex items-center justify-between w-full pr-2">
                    <div className="flex items-center gap-1 text-sm font-normal min-w-0 flex-1 overflow-hidden">
                      <div className="text-muted-foreground whitespace-nowrap flex-shrink-0">
                        Called tool{" "}
                      </div>
                      <div className="truncate flex-1 muted-foreground bg-muted py-1 px-1.5 rounded-sm text-xs max-w-fit">
                        <p className="truncate font-normal font-mono">
                          {toolTitle}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-accent-emerald-foreground">
                        {formatTime(toolDuration, true)}
                      </span>
                    </div>
                  </div>
                </AccordionTrigger>
                <AccordionContent className="pt-0">
                  <div className="text-sm text-muted-foreground px-4 pb-4 max-h-96 overflow-auto">
                    <ContentDisplay
                      playgroundPage={playgroundPage}
                      content={content}
                      chatId={`${chatId}-${blockIndex}-${contentIndex}`}
                    />
                  </div>

                  {/* Render nested blocks recursively INSIDE the accordion */}
                  {hasNestedBlocks && (
                    <div className="mt-2 ml-4 border-l-2 border-border pl-2">
                      {nestedToolItems.map(
                        (
                          { nestedBlock, toolKey: nestedToolKey },
                          nestedIdx,
                        ) => (
                          <NestedBlockRenderer
                            key={nestedToolKey}
                            block={nestedBlock}
                            blockIndex={nestedIdx}
                            toolKey={nestedToolKey}
                            toolElapsedTimes={toolElapsedTimes}
                            chatId={chatId}
                            playgroundPage={playgroundPage}
                            depth={depth + 1}
                          />
                        ),
                      )}
                    </div>
                  )}
                </AccordionContent>
              </AccordionItem>
            </div>
          );
        })}
    </div>
  );
}

export function ContentBlockDisplay({
  contentBlocks,
  isLoading,
  state,
  chatId,
  playgroundPage,
  hideHeader = false,
}: ContentBlockDisplayProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  // Hide INPUT and OUTPUT accordions for demo
  useEffect(() => {
    const hideInputOutputAccordions = () => {
      // Find all accordion items with tool names
      const accordionItems = document.querySelectorAll(".mb-2");
      accordionItems.forEach((item) => {
        const toolNameElement = item.querySelector(
          "p.truncate.font-normal.font-mono",
        );
        if (toolNameElement) {
          const toolName = toolNameElement.textContent?.trim().toUpperCase();
          if (toolName === "INPUT" || toolName === "OUTPUT") {
            (item as HTMLElement).classList.add("hide-input-output-accordion");
            console.log(
              `[ContentBlockDisplay] Hiding ${toolName} accordion via CSS`,
            );
          }
        }
      });
    };

    // Run immediately and after a short delay to catch dynamically rendered content
    hideInputOutputAccordions();
    const timer = setTimeout(hideInputOutputAccordions, 100);

    return () => clearTimeout(timer);
  }, [contentBlocks, isLoading]);

  // DEBUG: Log incoming content blocks
  console.log("[CONTENT BLOCK DISPLAY DEBUG] Component rendered:", {
    contentBlocksCount: contentBlocks?.length || 0,
    isLoading,
    state,
    chatId,
    contentBlocks: contentBlocks?.map((block) => ({
      title: block.title,
      blockType: block.block_type,
      contentsCount: block.contents.length,
      hasNestedBlocks: !!(
        block.nested_blocks && block.nested_blocks.length > 0
      ),
      nestedBlocksCount: block.nested_blocks?.length || 0,
      nestedBlockTitles: block.nested_blocks?.map((nb) => nb.title) || [],
    })),
  });

  // Use shared hook for tool duration tracking with ALL blocks (don't filter here)
  const { toolElapsedTimes, toolItems } = useToolDurations(
    contentBlocks,
    isLoading ?? false,
  );

  console.log("[CONTENT BLOCK DISPLAY DEBUG] Tool items processed:", {
    toolItemsCount: toolItems.length,
    toolItems: toolItems.map((item) => ({
      toolKey: item.toolKey,
      blockTitle: contentBlocks[item.blockIndex]?.title,
      hasNestedBlocks: !!contentBlocks[item.blockIndex]?.nested_blocks?.length,
    })),
  });

  if (!toolItems.length) {
    console.log("[CONTENT BLOCK DISPLAY DEBUG] No tool items, returning null");
    return null;
  }

  const totalDuration = isLoading
    ? undefined
    : toolItems.reduce((acc, { content, toolKey }) => {
        const toolDuration = toolElapsedTimes[toolKey] ?? content.duration ?? 0;
        return acc + toolDuration;
      }, 0);

  if (!contentBlocks?.length) {
    return null;
  }

  const headerIcon = state === "partial" ? "Bot" : "Check";
  const headerTitle = state === "partial" ? "Steps" : "Finished";

  return (
    <div className="relative py-3">
      {/* CSS to hide INPUT and OUTPUT accordions for demo */}
      <style>{`
        /* Hide accordion items that contain INPUT or OUTPUT in their tool name */
        .mb-2:has(p.truncate.font-normal.font-mono) {
          display: block;
        }
        /* Use JavaScript to hide - add class via useEffect */
        .hide-input-output-accordion {
          display: none !important;
        }
      `}</style>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{
          duration: 0.15,
          ease: "easeOut",
        }}
        className={cn("relative rounded-lg bg-transparent", "overflow-hidden")}
      >
        {isLoading && (
          <BorderTrail
            size={100}
            transition={{
              repeat: Infinity,
              duration: 10,
              ease: "linear",
            }}
          />
        )}
        {!hideHeader && (
          <div className="flex items-center justify-between p-4">
            <div className="flex items-center gap-2 align-baseline">
              {headerIcon && (
                <span data-testid="header-icon">
                  <ForwardedIconComponent
                    name={headerIcon}
                    className={cn(
                      "h-4 w-4",
                      state !== "partial" && "text-accent-emerald-foreground",
                    )}
                    strokeWidth={1.5}
                  />
                </span>
              )}
              <p className="m-0 flex items-center gap-2 text-sm font-semibold text-primary">
                {headerTitle}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {!playgroundPage && (
                <DurationDisplay duration={totalDuration} chatId={chatId} />
              )}
              <motion.div
                animate={{ rotate: isExpanded ? 180 : 0 }}
                transition={{ duration: 0.2, ease: "easeInOut" }}
                onClick={() => setIsExpanded((prev) => !prev)}
                className="cursor-pointer"
              >
                <ChevronDown className="h-5 w-5" />
              </motion.div>
            </div>
          </div>
        )}

        {(hideHeader || isExpanded) && (
          <div className="flex flex-col gap-2">
            <Accordion
              type="multiple"
              className="w-full bg-transparent flex flex-col gap-2"
            >
              {contentBlocks
                .filter((block, blockIndex) => {
                  // Log EVERY block before filtering
                  console.log(
                    "[ContentBlockDisplay] BLOCK-LEVEL filter - examining block:",
                    {
                      blockIndex,
                      blockTitle: block.title,
                      blockType: block.block_type,
                      contentsCount: block.contents?.length || 0,
                      hasNestedBlocks: !!(
                        block.nested_blocks && block.nested_blocks.length > 0
                      ),
                      nestedBlocksCount: block.nested_blocks?.length || 0,
                      chatId,
                    },
                  );

                  // Filter out INPUT and OUTPUT blocks when rendering (v2)
                  const blockTitle = block.title?.toUpperCase() || "";
                  const shouldFilter =
                    blockTitle === "INPUT" || blockTitle === "OUTPUT";

                  console.log(
                    "[ContentBlockDisplay] BLOCK-LEVEL filter decision:",
                    {
                      blockIndex,
                      title: block.title,
                      blockTitle,
                      shouldFilter,
                      willKeep: !shouldFilter,
                    },
                  );

                  return !shouldFilter;
                })
                .map((block, blockIndex) => (
                  <NestedBlockRenderer
                    key={`block-${blockIndex}`}
                    block={block}
                    blockIndex={blockIndex}
                    toolKey={`${blockIndex}`}
                    toolElapsedTimes={toolElapsedTimes}
                    chatId={chatId}
                    playgroundPage={playgroundPage}
                    depth={0}
                  />
                ))}
            </Accordion>
          </div>
        )}
      </motion.div>
    </div>
  );
}
