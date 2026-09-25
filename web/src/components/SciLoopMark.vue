<script setup lang="ts">
/**
 * SciLoop 标志（3 号方案：闭环 + 三节点）。
 *
 * 设计约束（2026-09-23 定稿）：
 * - **不用字母**（原来是 "SL" 字母块）；
 * - **单色**：只吃 `currentColor`，深底自动白标、浅底自动黑标，不做第三色；
 * - 48×48 网格 / 3 单位描边 → 缩到 16px 仍可辨认（favicon、对话头像都能用同一版）。
 * - 语义：闭合回路 = 流水线闭环；三个节点 = 关键节点可审计。
 */
withDefaults(defineProps<{ size?: number }>(), { size: 20 })
</script>

<template>
  <svg
    class="sl-mark"
    :width="size"
    :height="size"
    viewBox="0 0 48 48"
    role="img"
    aria-label="SciLoop"
  >
    <circle cx="24" cy="24" r="16" fill="none" stroke="currentColor" stroke-width="3" />
    <circle cx="24" cy="8" r="3.6" fill="currentColor" />
    <circle cx="37.9" cy="32" r="3.6" fill="currentColor" />
    <circle cx="10.1" cy="32" r="3.6" fill="currentColor" />
  </svg>
</template>

<style scoped>
.sl-mark {
  display: block;
  flex: none;
}
</style>
