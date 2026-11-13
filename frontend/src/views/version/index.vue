<template>
  <section class="page" data-module="version">
    <header class="page-head">
      <div>
        <h2>采集配置版本管理</h2>
        <p class="page-desc">维护采集配置的版本历史、变更记录与回滚控制，为采集配置管理提供可审计的数据工程流程。审计复核结论与归档标记会回写到本页，重启后仍可回看。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记配置版本</button>
        <button class="btn" type="button" @click="exportRows">导出采集配置版本清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">审计待核对总数</span>
        <strong class="stat-value" style="font-size: 16px">{{ pendingTotal }}</strong>
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
          <th>待核对数量</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="pill" :style="Number(row['待核对数量']) > 0 ? '' : 'background:#ecfdf3;color:#027a48'">
              {{ Number(row['待核对数量'] ?? 0) }}
            </span>
          </td>
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
            <button class="link" type="button" @click="openChecks(row)">核对视图</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集配置版本数据，可先登记配置版本</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集配置版本记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 审计核对视图：待核对项 + 审计回写流水（复核结论 / 归档标记） -->
    <div v-if="checkOpen" class="modal-mask" @click.self="checkOpen = false">
      <div class="modal-card">
        <div class="modal-head">
          <h3>审计核对视图 · {{ checkVersion }}</h3>
          <button class="modal-close" type="button" @click="checkOpen = false">×</button>
        </div>
        <h4 style="font-size: 13px">待核对项（{{ checkPending.length }}）</h4>
        <div v-if="!checkPending.length" class="empty-state" style="padding: 8px 0">暂无待核对项</div>
        <div v-for="item in checkPending" :key="String(item.事件)" class="event-line">
          <div class="event-meta">
            <span class="pill">{{ item['记录编号'] }}</span>
            <span> {{ item['时间'] }}</span>
          </div>
          <div>{{ item['说明'] }}</div>
        </div>
        <h4 style="font-size: 13px">审计回写（{{ checkWritten.length }}）</h4>
        <div v-if="!checkWritten.length" class="empty-state" style="padding: 8px 0">暂无回写记录</div>
        <div v-for="item in checkWritten" :key="String(item.事件)" class="event-line">
          <div class="event-meta">
            <span class="pill">{{ item['记录编号'] }}</span>
            <span> {{ item['时间'] }}</span>
          </div>
          <pre>{{ JSON.stringify(item, null, 2) }}</pre>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type CheckItem = {
  版本编号: string
  待核对项: Array<Record<string, unknown>>
  待核对数量: number
  审计回写: Array<Record<string, unknown>>
  回写数量: number
}

const ENDPOINT = '/api/version'
const columns = ["版本编号", "配置名称", "版本号", "变更说明", "变更内容", "回滚标识", "发布状态", "发布时间"]
const actions = ["发布版本", "回滚版本", "废弃版本"]
const statuses = ["草稿", "已发布", "已回滚", "已废弃"]
const stats = [{"label": "配置版本总数", "value": 0}, {"label": "已发布版本", "value": 0}, {"label": "已回滚版本", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const pendingTotal = ref(0)

const checkOpen = ref(false)
const checkVersion = ref('')
const checkPending = ref<Array<Record<string, unknown>>>([])
const checkWritten = ref<Array<Record<string, unknown>>>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '配置版本登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('采集配置版本动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集配置版本操作失败'
  }
}

async function openChecks(row: Row) {
  const versionNo = String(row['版本编号'] ?? '')
  checkVersion.value = versionNo
  checkPending.value = []
  checkWritten.value = []
  checkOpen.value = true
  try {
    const response = await request(`${ENDPOINT}/check-items?version_no=${encodeURIComponent(versionNo)}`)
    if (!response.ok) {
      throw new Error('核对视图读取失败')
    }
    const payload = await response.json()
    const item = (payload.items ?? []).find((v: CheckItem) => v.版本编号 === versionNo)
    checkPending.value = item?.待核对项 ?? []
    checkWritten.value = item?.审计回写 ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核对视图读取失败'
    checkOpen.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResp, checkResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/check-items`),
    ])
    if (!listResp.ok) {
      throw new Error('配置版本列表读取失败')
    }
    const payload = await listResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (checkResp.ok) {
      const checkPayload = await checkResp.json()
      pendingTotal.value = Number(checkPayload.待核对总数 ?? 0)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集配置版本列表读取失败'
  }
}

onMounted(reload)
</script>
