<script setup lang="ts">
/**
 * 技能库（独立入口）。
 *
 * 研究者 2026-09-23 定的口径：
 * - 装了什么、开没开、能不能真跑，一眼看全；
 * - 只有**非默认**才需要人管：默认全开，可以逐个关掉；关掉的技能**不会**被模型选中；
 * - 缺 API key 的技能要**明说"现在跑不了"**，别让人跑一半才发现；
 * - 能挂载外部技能目录，也能自己写一个技能（`SKILL.md`）。
 *
 * 「跑」不在这里：跑技能是执行类动作，走对话里的批准卡（卡上会列出将要执行的命令）。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import {
  fetchLibrary,
  fetchSkillDetail,
  runSkillHealthCheck,
  mountSkillsDir,
  saveSkill,
  setSkillEnabled,
  unmountSkillsDir,
  type SkillItem,
  type SkillLibrary,
} from '@/api/skills'
import { ApiError } from '@/api/client'

/**
 * 报错文案统一出口：**不把后端实现细节抛给研究者**。
 * 写操作被拒（浏览模式）要说清"现在不能做 + 去哪里开"，而不是回显请求头名字。
 */
function readableError(err: unknown, deniedAction: string): string {
  if (err instanceof ApiError && err.isForbidden) {
    return `${deniedAction}：当前是只读浏览模式，到「设置」里切换成研究者身份后重试。`
  }
  if (err instanceof Error) return err.message
  return String(err)
}

const library = ref<SkillLibrary | null>(null)
const loading = ref(false)
const errorText = ref('')
const noticeText = ref('')
const stage = ref<string>('全部')
const keyword = ref('')
const onlyRunnable = ref(false)
const busyName = ref('')
const healthBusy = ref(false)
/** 体检结论（跑完就更新；`null` = 还没体检过） */
const healthAt = ref('')
const healthError = ref('')

const mountDialog = reactive({ open: false, path: '', busy: false })
const editDialog = reactive({ open: false, name: '', content: '', busy: false, canEditName: true })

const TEMPLATE = `---
name: my-skill
description: 一句话说明这个技能什么时候用（模型只靠这句话决定要不要用它）
stage: literature
metadata:
  version: "1.0"
  license: MIT
---
# 我的技能

## 什么时候用
写清楚适用场景，以及**不适用**的场景。

## 怎么做
1. 第一步…
2. 第二步…
`

const stages = computed(() => ['全部', ...(library.value?.stages ?? []).map((item) => item.label)])

const visible = computed<SkillItem[]>(() => {
  const items = library.value?.items ?? []
  const text = keyword.value.trim().toLowerCase()
  return items.filter((item) => {
    if (stage.value !== '全部' && item.stage_label !== stage.value) return false
    if (onlyRunnable.value && !item.runnable) return false
    if (!text) return true
    return `${item.name} ${item.description}`.toLowerCase().includes(text)
  })
})

const counts = computed(() => {
  const items = library.value?.items ?? []
  return {
    total: items.length,
    enabled: items.filter((item) => item.enabled).length,
    runnable: items.filter((item) => item.runnable).length,
    instructions: items.filter((item) => item.mode === 'instructions').length,
  }
})

async function reload(): Promise<void> {
  loading.value = true
  errorText.value = ''
  try {
    library.value = await fetchLibrary()
  } catch (err) {
    errorText.value = readableError(err, '技能库读不出来')
  } finally {
    loading.value = false
  }
}

async function toggle(item: SkillItem): Promise<void> {
  busyName.value = item.name
  errorText.value = ''
  try {
    const next = await setSkillEnabled(item.name, !item.enabled)
    item.enabled = next.enabled
    noticeText.value = next.enabled ? `已启用「${item.name}」` : `已停用「${item.name}」（模型不会再选它）`
    await reload()
  } catch (err) {
    errorText.value = readableError(err, '改不动这个开关（要研究者身份）')
  } finally {
    busyName.value = ''
  }
}

async function submitMount(): Promise<void> {
  mountDialog.busy = true
  errorText.value = ''
  try {
    const result = await mountSkillsDir(mountDialog.path.trim())
    noticeText.value = result.message
    mountDialog.open = false
    mountDialog.path = ''
    if (result.library) library.value = result.library
    else await reload()
  } catch (err) {
    errorText.value = readableError(err, '挂载失败')
  } finally {
    mountDialog.busy = false
  }
}

async function removeMount(path: string): Promise<void> {
  try {
    await unmountSkillsDir(path)
    noticeText.value = `已取消挂载：${path}`
    await reload()
  } catch (err) {
    errorText.value = readableError(err, '取消挂载失败')
  }
}

async function openEditor(item?: SkillItem): Promise<void> {
  editDialog.open = true
  editDialog.name = item?.name ?? ''
  editDialog.canEditName = !item
  if (!item) {
    editDialog.content = TEMPLATE
    return
  }
  // ⚠️ 先读**当前原文**再进编辑器：否则改完一保存，原内容就被空壳覆盖了
  editDialog.content = '正在读取…'
  try {
    const detail = await fetchSkillDetail(item.name)
    editDialog.content = detail.content || TEMPLATE
  } catch (err) {
    errorText.value = readableError(err, '读不到这个技能的原文')
    editDialog.content = ''
  }
}

async function submitSkill(): Promise<void> {
  editDialog.busy = true
  errorText.value = ''
  try {
    const result = await saveSkill(editDialog.name.trim(), editDialog.content)
    noticeText.value = result.message
    editDialog.open = false
    await reload()
  } catch (err) {
    errorText.value = readableError(err, '保存失败')
  } finally {
    editDialog.busy = false
  }
}

function modeLabel(item: SkillItem): string {
  return item.mode === 'scripts' ? `可跑 ${item.steps} 步` : '说明书'
}


/**
 * 体检一次：算每个技能要用哪些第三方包，并在**研究者电脑上跑一条只读探针**看这些包与密钥在不在。
 *
 * 为什么要它：本机实测有过两种"跑到一半才炸"——缺 Python 包、缺 API key（还有包本身是坏的）。
 * 提前说清"这个技能现在跑不了、缺什么"，比跑一半报错强得多。
 */
async function runHealthCheck(): Promise<void> {
  healthBusy.value = true
  errorText.value = ''
  healthError.value = ''
  try {
    const result = await runSkillHealthCheck()
    healthAt.value = result.checked_at
    healthError.value = result.probe_error || ''
    noticeText.value = result.probe_error ? '体检没做成（见下方提示）' : '体检完成'
    await reload()
  } catch (err) {
    errorText.value = readableError(err, '体检没做成（要研究者身份）')
  } finally {
    healthBusy.value = false
  }
}

onMounted(reload)
</script>

<template>
  <section class="sk">
    <header class="sk__head">
      <div>
        <h1 class="sk__title">技能库</h1>
        <p class="sk__sub">
          装了 {{ counts.total }} 个 · 启用 {{ counts.enabled }} 个 · 能真跑 {{ counts.runnable }} 个
          <span v-if="counts.instructions">（其中 {{ counts.instructions }} 个是说明书型，不用跑脚本）</span>
        </p>
        <span v-if="healthAt"> · 上次体检 {{ healthAt.slice(0, 16).replace('T', ' ') }}</span>
      </div>
      <div class="sk__acts">
        <el-button size="small" :loading="healthBusy" @click="runHealthCheck">体检</el-button>
        <el-button size="small" @click="mountDialog.open = true">挂载技能目录</el-button>
        <el-button size="small" @click="openEditor()">新建技能</el-button>
      </div>
    </header>

    <div class="sk__filters">
      <div class="sk__stages">
        <button
          v-for="item in stages"
          :key="item"
          class="sk__chip"
          :class="{ 'sk__chip--on': stage === item }"
          type="button"
          @click="stage = item"
        >
          {{ item }}
        </button>
      </div>
      <el-input v-model="keyword" size="small" class="sk__search" placeholder="搜技能名或说明" clearable />
      <el-checkbox v-model="onlyRunnable" size="small" label="只看能真跑的" />
    </div>

    <p v-if="noticeText" class="sk__notice">{{ noticeText }}</p>
    <p v-if="errorText" class="sk__error">{{ errorText }}</p>
    <p v-if="healthError" class="sk__warn">{{ healthError }}</p>
    <p v-if="loading && !library" class="sk__hint">正在读技能库…</p>

    <ul class="sk__list">
      <li v-for="item in visible" :key="item.name" class="sk__row">
        <div class="sk__row-main">
          <div class="sk__row-title">
            <span class="sk__name">{{ item.name }}</span>
            <span class="sk__tag">{{ item.stage_label }}</span>
            <span class="sk__tag" :class="{ 'sk__tag--muted': item.mode === 'instructions' }">
              {{ modeLabel(item) }}
            </span>
            <span v-if="item.version" class="sk__tag sk__tag--muted">v{{ item.version }}</span>
          </div>
          <p class="sk__desc">{{ item.description }}</p>
          <!-- ⚠️ 只有**带脚本**的技能才谈"跑不了"；说明书型本来就不跑脚本，说它跑不了是误导 -->
          <p v-if="item.mode === 'scripts' && item.health && !item.health.can_run" class="sk__warn">
            现在跑不了：
            <template v-if="item.health.missing_python.length">缺包 {{ item.health.missing_python.join('、') }}</template>
            <template v-if="item.health.missing_python.length && item.health.missing_env.length">；</template>
            <template v-if="item.health.missing_env.length">缺 {{ item.health.missing_env.join('、') }}</template>
          </p>
          <p v-else-if="item.mode === 'scripts' && !item.health && item.missing_env.length" class="sk__warn">
            可能跑不了：缺 {{ item.missing_env.join('、') }}（点「体检」确认）
          </p>
          <p v-if="item.problems.length" class="sk__warn">{{ item.problems.join('；') }}</p>
        </div>
        <div class="sk__row-side">
          <el-switch
            :model-value="item.enabled"
            :loading="busyName === item.name"
            size="small"
            @change="toggle(item)"
          />
          <el-button size="small" text @click="openEditor(item)">编辑</el-button>
        </div>
      </li>
    </ul>
    <p v-if="!visible.length && !loading" class="sk__hint">这个筛选下没有技能。</p>

    <section v-if="library?.mounts?.length" class="sk__mounts">
      <h2 class="sk__h2">已挂载的外部目录</h2>
      <p v-for="path in library.mounts" :key="path" class="sk__mount">
        <span class="sk__mount-path">{{ path }}</span>
        <el-button size="small" text @click="removeMount(path)">取消挂载</el-button>
      </p>
    </section>

    <p class="sk__foot">
      技能清单会**只以「名字 + 一句话」**给模型看；它选中哪个才加载全文，再按技能声明的流程做事。
      需要跑脚本时会在对话里先请你确认（卡上列出将要执行的命令），产物落到项目产物目录。
    </p>

    <el-dialog v-model="mountDialog.open" title="挂载外部技能目录" width="520px">
      <p class="sk__hint">填一个目录：里面每个含 `SKILL.md` 的子目录都会被当成一个技能（同名以内置的为准）。</p>
      <el-input v-model="mountDialog.path" placeholder="例如 D:/my-skills" />
      <template #footer>
        <el-button @click="mountDialog.open = false">取消</el-button>
        <el-button type="primary" :loading="mountDialog.busy" @click="submitMount">挂载</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editDialog.open" :title="editDialog.canEditName ? '新建技能' : '编辑技能'" width="720px">
      <el-input v-model="editDialog.name" :disabled="!editDialog.canEditName" placeholder="技能名（英文小写与连字符）" />
      <el-input
        v-model="editDialog.content"
        class="sk__editor"
        type="textarea"
        :rows="18"
        spellcheck="false"
        placeholder="SKILL.md 内容"
      />
      <template #footer>
        <el-button @click="editDialog.open = false">取消</el-button>
        <el-button type="primary" :loading="editDialog.busy" @click="submitSkill">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.sk {
  padding: 24px 28px 40px;
  color: var(--h-fg);
}

.sk__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.sk__title {
  margin: 0;
  font-size: 18px;
  font-weight: 500;
}

.sk__sub {
  margin: 6px 0 0;
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
}

.sk__acts {
  display: flex;
  gap: 8px;
}

.sk__filters {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 18px 0 12px;
  flex-wrap: wrap;
}

.sk__stages {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.sk__chip {
  padding: 4px 10px;
  border: 1px solid var(--h-line);
  border-radius: 999px;
  background: transparent;
  color: var(--h-fg-muted);
  font-family: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: all 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.sk__chip:hover {
  background: var(--h-hover);
}

.sk__chip--on {
  border-color: var(--h-fg);
  color: var(--h-fg);
  font-weight: 500;
}

.sk__chip:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 2px;
}

.sk__search {
  max-width: 260px;
}

.sk__notice,
.sk__hint,
.sk__foot {
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
}

.sk__notice {
  margin: 0 0 10px;
}

.sk__error {
  margin: 0 0 10px;
  color: var(--h-fg);
  font-size: var(--font-size-sm);
}

.sk__list {
  margin: 0;
  padding: 0;
  list-style: none;
  border-top: 1px solid var(--h-line);
}

.sk__row {
  display: flex;
  gap: 16px;
  padding: 14px 2px;
  border-bottom: 1px solid var(--h-line);
}

.sk__row-main {
  flex: 1;
  min-width: 0;
}

.sk__row-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.sk__name {
  font-weight: 500;
}

.sk__tag {
  padding: 2px 8px;
  border: 1px solid var(--h-line);
  border-radius: 999px;
  color: var(--h-fg-muted);
  font-size: 12px;
}

.sk__tag--muted {
  border-style: dashed;
}

.sk__desc {
  margin: 6px 0 0;
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
  line-height: 1.6;
}

.sk__warn {
  margin: 6px 0 0;
  color: var(--h-fg);
  font-size: 12px;
}

.sk__row-side {
  display: flex;
  align-items: center;
  gap: 8px;
}

.sk__mounts {
  margin-top: 22px;
}

.sk__h2 {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 500;
}

.sk__mount {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 4px;
  font-size: var(--font-size-sm);
}

.sk__mount-path {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  overflow-wrap: anywhere;
}

.sk__foot {
  margin: 24px 0 0;
  line-height: 1.7;
}

.sk__editor {
  margin-top: 10px;
}
</style>
