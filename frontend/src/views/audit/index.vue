<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>采集审计日志管理</h2>
        <p class="page-desc">维护采集审计的操作类型、执行记录与复核状态，为采集合规审计提供可追溯的数据工程流程。记录已切换为追加式持久存储，重启后复核与归档标记不丢失。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记审计记录</button>
        <button class="btn" type="button" @click="exportRows">导出采集审计日志清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">迁移状态</span>
        <strong class="stat-value" style="font-size: 14px">{{ migrationText }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openAmend(row)">追加修正</button>
            <button class="link" type="button" @click="openEvents(row)">事件流水</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集审计日志数据，可先登记审计记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集审计日志记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 追加修正弹层：历史不改写，只新增一条追加事件 -->
    <div v-if="amendOpen" class="modal-mask" @click.self="amendOpen = false">
      <div class="modal-card">
        <div class="modal-head">
          <h3>追加修正 · {{ amendTarget?.['记录编号'] }}</h3>
          <button class="modal-close" type="button" @click="amendOpen = false">×</button>
        </div>
        <div class="modal-form">
          <label>修正字段</label>
          <select v-model="amendField">
            <option v-for="field in amendableFields" :key="field" :value="field">{{ field }}</option>
          </select>
          <label>修正后的值</label>
          <textarea v-model="amendValue" rows="2" placeholder="仅追加新事件，不会覆盖历史记录" />
          <label>修正说明（可选）</label>
          <input v-model="amendNote" placeholder="例如：人工补录、迁移核对" />
        </div>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="amendOpen = false">取消</button>
          <button class="btn primary" type="button" @click="submitAmend">提交追加修正</button>
        </div>
      </div>
    </div>

    <!-- 事件流水弹层：同一条记录的重放事件按 seq 稳定排序，无副本 -->
    <div v-if="eventsOpen" class="modal-mask" @click.self="eventsOpen = false">
      <div class="modal-card">
        <div class="modal-head">
          <h3>追加事件流水 · {{ eventsTarget?.['记录编号'] }}</h3>
          <button class="modal-close" type="button" @click="eventsOpen = false">×</button>
        </div>
        <div v-if="!eventItems.length" class="empty-state">暂无事件</div>
        <div v-for="event in eventItems" :key="String(event.seq)" class="event-line">
          <div class="event-meta">
            <span class="pill">#{{ event.seq }} {{ event.type }}</span>
            <span> {{ event.ts }}</span>
          </div>
          <pre>{{ JSON.stringify(event.payload, null, 2) }}</pre>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type AuditEvent = { seq: number; id: string; ts: string; type: string; payload: Record<string, unknown> }
type MigrationStatus = { 状态: string; 已迁移: number; 总量: number; 失败区间: Array<Record<string, unknown>> }

const ENDPOINT = '/api/audit'
const columns = ["记录编号", "操作类型", "采集计划", "执行人", "执行时间", "结果摘要", "变更内容", "复核状态"]
const actions = ["标记复核", "归档记录"]
const statuses = ["已记录", "已复核", "已归档"]
const stats = [{"label": "审计记录总数", "value": 0}, {"label": "已复核记录", "value": 0}, {"label": "已归档记录", "value": 0}]
const amendableFields = ["操作类型", "采集计划", "执行人", "执行时间", "结果摘要", "变更内容", "复核状态"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const migrationText = ref('读取中…')

const amendOpen = ref(false)
const amendTarget = ref<Row | null>(null)
const amendField = ref(amendableFields[0])
const amendValue = ref('')
const amendNote = ref('')

const eventsOpen = ref(false)
const eventsTarget = ref<Row | null>(null)
const eventItems = ref<AuditEvent[]>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '审计记录登记入口尚未接入审批流'
}

async function loadMigrationStatus() {
  try {
    const response = await request(`${ENDPOINT}/migration-status`)
    if (!response.ok) {
      migrationText.value = '未知'
      return
    }
    const data = (await response.json()) as MigrationStatus
    const failed = data.失败区间?.length ? `，失败区间 ${data.失败区间.length} 段` : ''
    migrationText.value = `${data.状态}（${data.已迁移}/${data.总量}）${failed}`
  } catch {
    migrationText.value = '未知'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('采集审计日志动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集审计日志操作失败'
  }
}

function openAmend(row: Row) {
  amendTarget.value = row
  amendField.value = amendableFields[0]
  amendValue.value = ''
  amendNote.value = ''
  amendOpen.value = true
}

async function submitAmend() {
  if (!amendTarget.value || !amendValue.value.trim()) {
    errorMessage.value = '请填写修正后的值'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${amendTarget.value.id}/amend`, {
      method: 'POST',
      body: JSON.stringify({
        values: { [amendField.value]: amendValue.value, note: amendNote.value },
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? '追加修正未生效')
    }
    amendOpen.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '追加修正失败'
  }
}

async function openEvents(row: Row) {
  eventsTarget.value = row
  eventItems.value = []
  eventsOpen.value = true
  try {
    const response = await request(`${ENDPOINT}/${row.id}/events`)
    if (!response.ok) {
      throw new Error('事件流水读取失败')
    }
    const payload = await response.json()
    eventItems.value = (payload.items ?? []) as AuditEvent[]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '事件流水读取失败'
    eventsOpen.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('审计记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集审计日志列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadMigrationStatus()
})
</script>
