/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 页内标签与 URL 查询参数双向绑定：刷新 / 分享 / 后退都能回到同一个标签。
 * 只认白名单内的值，非法值回落默认；用 replace 而非 push，避免塞满历史记录。
 */

import { ref, watch, type Ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

export function useQueryTab<T extends string>(
  key: string,
  allowed: ReadonlyArray<T>,
  fallback: T,
): Ref<T> {
  const route = useRoute()
  const router = useRouter()

  const read = (): T => {
    const raw = route.query[key]
    const value = Array.isArray(raw) ? raw[0] : raw
    return allowed.includes(value as T) ? (value as T) : fallback
  }

  const current = ref<T>(read()) as Ref<T>

  watch(
    () => route.query[key],
    () => {
      const next = read()
      if (next !== current.value) current.value = next
    },
  )

  watch(current, (value) => {
    if (read() === value) return
    void router.replace({ query: { ...route.query, [key]: value } })
  })

  return current
}
